import asyncio
import hashlib
import json
import logging
from pathlib import Path
import tempfile
import unittest

import httpx

from backend.llm import ContextLimitError, LLM, StoryAnalysisError
from backend.story import (
    AnalysisKey,
    AnalysisStore,
    ImportedStory,
    StoryAnalyzer,
    StoryChapter,
    StoryMap,
    StoryScene,
)


class FakeStructuredClient:
    """Deterministic replacement for the one external structured-JSON call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    @property
    def call_count(self):
        return len(self.calls)

    async def complete_json(self, system_prompt, user_content, schema_name):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_content": user_content,
                "schema_name": schema_name,
            }
        )
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def imported_story(text, filename="story.txt"):
    original_bytes = text.encode("utf-8")
    return ImportedStory(
        filename=filename,
        extension=Path(filename).suffix,
        original_bytes=original_bytes,
        text=text,
        source_sha256=hashlib.sha256(original_bytes).hexdigest(),
    )


class StoryAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.story = imported_story("第一章\n甲乙\n第二章\n丙丁")
        self.key = AnalysisKey(
            source_hash=self.story.source_sha256,
            model="test-model",
            prompt_version="faithful-v1",
            dlc_version="test-dlc-v1",
        )
        self.store = AnalysisStore(self.temporary_directory.name)

    def analyzer(self, client, *, max_chunk_chars=8):
        return StoryAnalyzer(
            client=client,
            store=self.store,
            key=self.key,
            max_chunk_chars=max_chunk_chars,
        )

    def complete_map(self):
        source_hash = self.story.source_sha256
        return StoryMap(
            source_hash=source_hash,
            text_length=13,
            chapters=(
                StoryChapter(
                    id=StoryChapter.stable_id(source_hash, 0),
                    index=0,
                    start_offset=0,
                    end_offset=7,
                    title="第一章",
                    summary="第一部分",
                    scenes=(
                        StoryScene(
                            id=StoryScene.stable_id(source_hash, 0, 0),
                            index=0,
                            start_offset=0,
                            end_offset=7,
                            summary="第一幕",
                            pace=1.0,
                        ),
                    ),
                ),
                StoryChapter(
                    id=StoryChapter.stable_id(source_hash, 1),
                    index=1,
                    start_offset=7,
                    end_offset=13,
                    title="第二章",
                    summary="第二部分",
                    scenes=(
                        StoryScene(
                            id=StoryScene.stable_id(source_hash, 1, 0),
                            index=0,
                            start_offset=7,
                            end_offset=13,
                            summary="第二幕",
                            pace=1.25,
                        ),
                    ),
                ),
            ),
        )

    async def test_cache_hit_returns_complete_map_without_calling_llm(self):
        # Catches loading after calling the model or ignoring a reusable cache entry.
        expected = self.complete_map()
        self.store.save(self.key, expected)
        client = FakeStructuredClient([AssertionError("LLM must not be called")])

        result = await self.analyzer(client).analyze(self.story)

        self.assertEqual(result, expected)
        self.assertEqual(client.call_count, 0)

    async def test_whole_book_response_yields_source_namespaced_stable_ids(self):
        # Catches IDs derived from summaries or truncated source hashes.
        client = FakeStructuredClient(
            [
                {
                    "chapters": [
                        {
                            "start": 0,
                            "end": 7,
                            "title": "第一章",
                            "summary": "第一部分",
                            "scenes": [
                                {
                                    "start": 0,
                                    "end": 7,
                                    "summary": "第一幕",
                                    "pace": 1.0,
                                }
                            ],
                        },
                        {
                            "start": 7,
                            "end": 13,
                            "title": "第二章",
                            "summary": "第二部分",
                            "scenes": [
                                {
                                    "start": 7,
                                    "end": 13,
                                    "summary": "第二幕",
                                    "pace": 1.25,
                                }
                            ],
                        },
                    ]
                }
            ]
        )

        result = await self.analyzer(client).analyze(self.story)

        source_hash = self.story.source_sha256
        self.assertEqual(result, self.complete_map())
        self.assertEqual(result.text_length, 13)
        self.assertEqual(
            [chapter.id for chapter in result.chapters],
            [f"ch-{source_hash}-0001", f"ch-{source_hash}-0002"],
        )
        self.assertEqual(
            [chapter.scenes[0].id for chapter in result.chapters],
            [
                f"ch-{source_hash}-0001-sc-0001",
                f"ch-{source_hash}-0002-sc-0001",
            ],
        )
        self.assertEqual(client.call_count, 1)
        self.assertEqual(client.calls[0]["user_content"], self.story.text)
        self.assertEqual(self.store.load(self.key), result)

    async def test_context_limit_uses_detected_heading_chunks_with_global_offsets(self):
        # Catches size fallback being chosen before headings or local offsets leaking into the merge.
        client = FakeStructuredClient(
            [
                ContextLimitError("request exceeds context"),
                {
                    "scenes": [
                        {"start": 0, "end": 7, "summary": "第一幕", "pace": 1.0}
                    ]
                },
                {
                    "scenes": [
                        {"start": 0, "end": 6, "summary": "第二幕", "pace": 1.5}
                    ]
                },
            ]
        )

        result = await self.analyzer(client, max_chunk_chars=4).analyze(self.story)

        self.assertEqual(client.call_count, 3)
        self.assertEqual(
            [call["user_content"] for call in client.calls],
            [self.story.text, "第一章\n甲乙\n", "第二章\n丙丁"],
        )
        self.assertEqual(
            [(chapter.start_offset, chapter.end_offset) for chapter in result.chapters],
            [(0, 7), (7, 13)],
        )
        self.assertEqual(
            [
                (chapter.scenes[0].start_offset, chapter.scenes[0].end_offset)
                for chapter in result.chapters
            ],
            [(0, 7), (7, 13)],
        )
        self.assertEqual([chapter.title for chapter in result.chapters], ["第一章", "第二章"])
        self.assertEqual([chapter.summary for chapter in result.chapters], ["第一幕", "第二幕"])

    async def test_heading_free_source_uses_paragraph_bound_chunks_without_character_loss(self):
        # Catches dropped paragraph separators, overlap, and chunks above the configured bound.
        story = imported_story("甲乙\n\n丙丁\n\n戊己")
        key = AnalysisKey(story.source_sha256, "test-model", "faithful-v1", "test-dlc-v1")
        analyzer = StoryAnalyzer(
            client=FakeStructuredClient(
                [
                    ContextLimitError("too long"),
                    {
                        "scenes": [
                            {"start": 0, "end": 4, "summary": "甲乙", "pace": 0.75}
                        ]
                    },
                    {
                        "scenes": [
                            {"start": 0, "end": 6, "summary": "丙丁戊己", "pace": 1.2}
                        ]
                    },
                ]
            ),
            store=self.store,
            key=key,
            max_chunk_chars=6,
        )

        result = await analyzer.analyze(story)

        sent = [call["user_content"] for call in analyzer.client.calls[1:]]
        self.assertEqual(sent, ["甲乙\n\n", "丙丁\n\n戊己"])
        self.assertEqual("".join(sent), story.text)
        self.assertTrue(all(0 < len(chunk) <= 6 for chunk in sent))
        self.assertEqual(
            [(chapter.start_offset, chapter.end_offset) for chapter in result.chapters],
            [(0, 4), (4, 10)],
        )
        self.assertEqual(result.text_length, 10)

    async def test_markdown_chapter_headings_select_heading_fallback(self):
        # Catches Markdown heading markers hiding otherwise valid chapter boundaries.
        story = imported_story("# 第一章 开端\n甲\n# 第二章 继续\n乙", filename="story.md")
        key = AnalysisKey(story.source_sha256, "test-model", "faithful-v1", "test-dlc-v1")
        client = FakeStructuredClient(
            [
                ContextLimitError("too long"),
                {"scenes": [{"start": 0, "end": 11, "summary": "开端", "pace": 1}]},
                {"scenes": [{"start": 0, "end": 10, "summary": "继续", "pace": 1}]},
            ]
        )
        analyzer = StoryAnalyzer(client, self.store, key, max_chunk_chars=3)

        result = await analyzer.analyze(story)

        self.assertEqual(
            [call["user_content"] for call in client.calls[1:]],
            ["# 第一章 开端\n甲\n", "# 第二章 继续\n乙"],
        )
        self.assertEqual([chapter.title for chapter in result.chapters], ["第一章 开端", "第二章 继续"])

    async def test_context_limit_in_fallback_chunk_is_not_recursively_split(self):
        # Catches an unrequested retry/recursive splitting framework after fallback begins.
        client = FakeStructuredClient(
            [ContextLimitError("whole book"), ContextLimitError("chapter too long")]
        )

        with self.assertRaises(ContextLimitError):
            await self.analyzer(client).analyze(self.story)

        self.assertEqual(client.call_count, 2)
        self.assertIsNone(self.store.load(self.key))

    async def test_oversized_paragraph_is_hard_bounded_without_gaps_or_overlap(self):
        # Catches an unbounded single paragraph bypassing the configured character limit.
        story = imported_story("甲乙丙丁戊己庚")
        key = AnalysisKey(story.source_sha256, "test-model", "faithful-v1", "test-dlc-v1")
        client = FakeStructuredClient(
            [
                ContextLimitError("too long"),
                {"scenes": [{"start": 0, "end": 3, "summary": "甲乙丙", "pace": 1}]},
                {"scenes": [{"start": 0, "end": 3, "summary": "丁戊己", "pace": 1}]},
                {"scenes": [{"start": 0, "end": 1, "summary": "庚", "pace": 1}]},
            ]
        )
        analyzer = StoryAnalyzer(client, self.store, key, max_chunk_chars=3)

        result = await analyzer.analyze(story)

        chunks = [call["user_content"] for call in client.calls[1:]]
        self.assertEqual(chunks, ["甲乙丙", "丁戊己", "庚"])
        self.assertEqual("".join(chunks), story.text)
        self.assertEqual(
            [(chapter.start_offset, chapter.end_offset) for chapter in result.chapters],
            [(0, 3), (3, 6), (6, 7)],
        )

    async def test_size_fallback_does_not_emit_whitespace_only_chunk(self):
        # Catches a boundary immediately at the cursor becoming an empty-content chapter.
        story = imported_story("abc\n\ndef")
        key = AnalysisKey(story.source_sha256, "test-model", "faithful-v1", "test-dlc-v1")
        client = FakeStructuredClient(
            [
                ContextLimitError("too long"),
                {"scenes": [{"start": 0, "end": 3, "summary": "abc", "pace": 1}]},
                {"scenes": [{"start": 0, "end": 3, "summary": "d", "pace": 1}]},
                {"scenes": [{"start": 0, "end": 2, "summary": "ef", "pace": 1}]},
            ]
        )
        analyzer = StoryAnalyzer(client, self.store, key, max_chunk_chars=3)

        await analyzer.analyze(story)

        chunks = [call["user_content"] for call in client.calls[1:]]
        self.assertEqual(chunks, ["abc", "\n\nd", "ef"])
        self.assertEqual("".join(chunks), story.text)
        self.assertTrue(all(chunk.strip() for chunk in chunks))

    async def test_invalid_chunk_fails_entire_analysis_without_cache_write(self):
        # Catches partial cache persistence when one chunk leaves an offset gap.
        client = FakeStructuredClient(
            [
                ContextLimitError("too long"),
                {
                    "scenes": [
                        {"start": 0, "end": 7, "summary": "第一幕", "pace": 1.0}
                    ]
                },
                {
                    "scenes": [
                        {"start": 1, "end": 6, "summary": "第二幕", "pace": 1.0}
                    ]
                },
            ]
        )

        with self.assertRaises(StoryAnalysisError):
            await self.analyzer(client).analyze(self.story)

        self.assertIsNone(self.store.load(self.key))

    async def test_invalid_whole_book_partition_does_not_silently_chunk(self):
        # Catches treating model/schema errors as provider context-limit errors.
        client = FakeStructuredClient(
            [
                {
                    "chapters": [
                        {
                            "start": 1,
                            "end": 13,
                            "title": "bad",
                            "summary": "bad",
                            "scenes": [
                                {"start": 1, "end": 13, "summary": "bad", "pace": 1}
                            ],
                        }
                    ]
                }
            ]
        )

        with self.assertRaises(StoryAnalysisError):
            await self.analyzer(client).analyze(self.story)

        self.assertEqual(client.call_count, 1)
        self.assertIsNone(self.store.load(self.key))

    async def test_non_context_failure_is_retryable_and_does_not_chunk(self):
        # Catches silent fallback after authentication, rate-limit, or transport failures.
        client = FakeStructuredClient([StoryAnalysisError("provider unavailable")])

        with self.assertRaises(StoryAnalysisError) as raised:
            await self.analyzer(client).analyze(self.story)

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(client.call_count, 1)
        self.assertIsNone(self.store.load(self.key))

    async def test_cancellation_propagates_without_writing_cache(self):
        # Catches cancellation being wrapped, retried, chunked, or persisted as partial success.
        client = FakeStructuredClient([asyncio.CancelledError()])

        with self.assertRaises(asyncio.CancelledError):
            await self.analyzer(client).analyze(self.story)

        self.assertEqual(client.call_count, 1)
        self.assertIsNone(self.store.load(self.key))

    async def test_model_values_require_nonempty_summaries_and_finite_positive_pace(self):
        # Catches NaN/zero pace or whitespace summaries escaping generated-output validation.
        invalid_scenes = (
            {"start": 0, "end": 13, "summary": " ", "pace": 1},
            {"start": 0, "end": 13, "summary": "scene", "pace": 0},
            {"start": 0, "end": 13, "summary": "scene", "pace": float("nan")},
        )
        for scene in invalid_scenes:
            with self.subTest(scene=scene):
                client = FakeStructuredClient([{"scenes": [scene]}])
                with self.assertRaises(StoryAnalysisError):
                    await self.analyzer(client).analyze(self.story)
                self.assertIsNone(self.store.load(self.key))


class StructuredLLMTests(unittest.IsolatedAsyncioTestCase):
    def llm_with_response(self, status_code, payload):
        async def handle(request):
            return httpx.Response(status_code, json=payload, request=request)

        llm = object.__new__(LLM)
        llm.url = "https://example.invalid/chat/completions"
        llm.api_key = "test-key"
        llm.model = "test-model"
        llm.temperature = 0.5
        llm.max_tokens = 1000
        llm.reasoning_effort = ""
        llm.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(llm.client.aclose)
        return llm

    async def test_complete_json_returns_one_structured_object(self):
        # Catches returning raw strings or omitting JSON mode from the structured seam.
        captured = {}

        async def handle(request):
            captured.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"scenes": []}'}}]},
                request=request,
            )

        llm = object.__new__(LLM)
        llm.url = "https://example.invalid/chat/completions"
        llm.api_key = "test-key"
        llm.model = "test-model"
        llm.temperature = 0.5
        llm.max_tokens = 1000
        llm.reasoning_effort = "low"
        llm.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(llm.client.aclose)

        result = await llm.complete_json("system", "private novel", "story_map")

        self.assertEqual(result, {"scenes": []})
        self.assertEqual(captured["messages"][1], {"role": "user", "content": "private novel"})
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["reasoning"], {"effort": "low", "exclude": True})

    async def test_successful_structured_response_is_not_capped_by_error_inspection_bound(self):
        # Catches the defensive error-body bound truncating a valid large scene map.
        summary = "s" * (70 * 1024)
        llm = self.llm_with_response(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"scenes": [{"summary": summary}]},
                                separators=(",", ":"),
                            )
                        }
                    }
                ]
            },
        )

        result = await llm.complete_json("system", "private novel", "story_map")

        self.assertEqual(result["scenes"][0]["summary"], summary)

    async def test_verified_error_code_classifies_context_limit(self):
        # Catches missing OpenAI/OpenRouter context-length error classification.
        llm = self.llm_with_response(
            400,
            {
                "error": {
                    "message": "This model's maximum context length was exceeded.",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                }
            },
        )

        with self.assertRaises(ContextLimitError):
            await llm.complete_json("system", "private novel", "story_map")

    async def test_bounded_openrouter_provider_error_classifies_context_limit(self):
        # Catches missing OpenRouter provider errors without scanning unverified body text.
        llm = self.llm_with_response(
            400,
            {
                "error": {
                    "message": "Provider returned error",
                    "code": 400,
                    "metadata": {
                        "provider_name": "test-provider",
                        "raw": json.dumps(
                            {
                                "error": {
                                    "message": "maximum context length exceeded",
                                    "type": "invalid_request_error",
                                    "code": "context_length_exceeded",
                                }
                            }
                        ),
                    },
                }
            },
        )

        with self.assertRaises(ContextLimitError):
            await llm.complete_json("system", "private novel", "story_map")

    async def test_context_marker_outside_verified_error_fields_is_not_context_limit(self):
        # Catches scanning an arbitrary response body for context keywords.
        llm = self.llm_with_response(400, {"message": "context_length_exceeded"})

        with self.assertRaises(StoryAnalysisError) as raised:
            await llm.complete_json("system", "private novel", "story_map")

        self.assertNotIsInstance(raised.exception, ContextLimitError)
        self.assertTrue(raised.exception.retryable)

    async def test_non_context_http_and_malformed_success_are_story_analysis_errors(self):
        # Catches leaking transport/JSON exceptions outside the retryable analysis contract.
        cases = (
            (429, {"error": {"message": "rate limited", "type": "rate_limit", "code": 429}}),
            (200, {"choices": [{"message": {"content": "not-json"}}]}),
        )
        for status_code, payload in cases:
            with self.subTest(status_code=status_code):
                llm = self.llm_with_response(status_code, payload)
                with self.assertRaises(StoryAnalysisError) as raised:
                    await llm.complete_json("system", "private novel", "story_map")
                self.assertTrue(raised.exception.retryable)

    async def test_structured_failure_does_not_log_or_raise_source_content(self):
        # Catches source text leaking through exception messages or model-call logs.
        source = "PRIVATE-NOVEL-CONTENT-UNIQUE"
        llm = self.llm_with_response(
            500,
            {"error": {"message": f"provider echoed {source}", "type": "server_error"}},
        )

        with self.assertLogs("ai-for-coyote.llm", level=logging.DEBUG) as captured:
            with self.assertRaises(StoryAnalysisError) as raised:
                await llm.complete_json("system", source, "story_map")

        self.assertNotIn(source, str(raised.exception))
        self.assertNotIn(source, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
