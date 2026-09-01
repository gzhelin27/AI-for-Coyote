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

    def __init__(self, responses, *, model="test-model"):
        self._responses = list(responses)
        self.calls = []
        self.model = model

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


class PartitioningStructuredClient:
    """Context-fail once, then return an exact one-scene partition for each chunk."""

    model = "test-model"

    def __init__(self):
        self.calls = []

    async def complete_json(self, system_prompt, user_content, schema_name):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_content": user_content,
                "schema_name": schema_name,
            }
        )
        if schema_name == "story_map":
            raise ContextLimitError("whole book too long")
        return {
            "scenes": [
                {
                    "start": 0,
                    "end": len(user_content),
                    "summary": user_content.strip() or "whitespace",
                    "pace": 1,
                }
            ]
        }


class BlockingStructuredClient:
    model = "test-model"

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

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
        self.started.set()
        await self.release.wait()
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class ObservedAnalysisStore(AnalysisStore):
    def __init__(self, directory):
        super().__init__(directory)
        self.save_count = 0
        self.load_observed = None
        self.save_observed = None

    def load(self, key):
        result = super().load(key)
        if self.load_observed is not None:
            self.load_observed.set()
        return result

    def save(self, key, story_map):
        self.save_count += 1
        super().save(key, story_map)
        if self.save_observed is not None:
            self.save_observed.set()


class PopulateOnFirstMissStore(ObservedAnalysisStore):
    def __init__(self, directory, story_map):
        super().__init__(directory)
        self.story_map = story_map
        self.seeded = False

    def load(self, key):
        result = super().load(key)
        if result is None and not self.seeded:
            self.seeded = True
            self.save(key, self.story_map)
            return None
        return result


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
            prompt_version="faithful-v1",
            dlc_version="test-dlc-v1",
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

    def one_scene_response(self, summary="shared result"):
        return {
            "scenes": [
                {
                    "start": 0,
                    "end": len(self.story.text),
                    "summary": summary,
                    "pace": 1,
                }
            ]
        }

    def analyzer_with(self, client, store):
        return StoryAnalyzer(
            client,
            store,
            "faithful-v1",
            "test-dlc-v1",
            max_chunk_chars=8,
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
        self.assertIn("faithful-v1", client.calls[0]["system_prompt"])
        self.assertEqual(self.store.load(self.key), result)

    async def test_effective_model_selects_cache_identity_without_caller_key(self):
        # Catches a caller-supplied/stale key reusing or overwriting another model's cache.
        old_map = self.complete_map()
        self.store.save(self.key, old_map)
        client = FakeStructuredClient(
            [
                {
                    "scenes": [
                        {"start": 0, "end": 13, "summary": "new model", "pace": 1}
                    ]
                }
            ],
            model="different-model",
        )
        analyzer = self.analyzer(client)

        result = await analyzer.analyze(self.story)

        effective_key = AnalysisKey(
            self.story.source_sha256,
            "different-model",
            "faithful-v1",
            "test-dlc-v1",
        )
        self.assertEqual(client.call_count, 1)
        self.assertEqual(result.chapters[0].summary, "new model")
        self.assertEqual(self.store.load(self.key), old_map)
        self.assertEqual(self.store.load(effective_key), result)

    async def test_concurrent_analyzers_share_one_model_call_save_and_result(self):
        # Catches analyzer-instance-local ownership duplicating a paid analysis and cache write.
        owner_store = ObservedAnalysisStore(self.temporary_directory.name)
        join_store = ObservedAnalysisStore(self.temporary_directory.name)
        join_store.load_observed = asyncio.Event()
        owner_client = BlockingStructuredClient(self.one_scene_response())
        join_client = FakeStructuredClient([AssertionError("joiner must not call model")])
        owner = self.analyzer_with(owner_client, owner_store)
        joiner = self.analyzer_with(join_client, join_store)

        owner_call = asyncio.create_task(owner.analyze(self.story))
        await owner_client.started.wait()
        joined_call = asyncio.create_task(joiner.analyze(self.story))
        await join_store.load_observed.wait()
        owner_client.release.set()
        owner_result, joined_result = await asyncio.gather(owner_call, joined_call)

        self.assertIs(owner_result, joined_result)
        self.assertEqual(owner_client.call_count, 1)
        self.assertEqual(join_client.call_count, 0)
        self.assertEqual(owner_store.save_count + join_store.save_count, 1)

    async def test_new_owner_double_checks_cache_after_outer_miss(self):
        # Catches a cache populated between the caller lookup and owner start being re-analyzed.
        expected = self.complete_map()
        store = PopulateOnFirstMissStore(self.temporary_directory.name, expected)
        client = FakeStructuredClient([AssertionError("owner must recheck cache")])

        result = await self.analyzer_with(client, store).analyze(self.story)

        self.assertEqual(result, expected)
        self.assertEqual(client.call_count, 0)
        self.assertEqual(store.save_count, 1)

    async def test_cancelling_joiner_does_not_cancel_shared_owner(self):
        # Catches cancellation propagating through a joiner's await into the paid owner task.
        owner_store = ObservedAnalysisStore(self.temporary_directory.name)
        join_store = ObservedAnalysisStore(self.temporary_directory.name)
        join_store.load_observed = asyncio.Event()
        owner_client = BlockingStructuredClient(self.one_scene_response())
        owner = self.analyzer_with(owner_client, owner_store)
        joiner = self.analyzer_with(
            FakeStructuredClient([AssertionError("joiner must not call model")]),
            join_store,
        )

        owner_call = asyncio.create_task(owner.analyze(self.story))
        await owner_client.started.wait()
        joined_call = asyncio.create_task(joiner.analyze(self.story))
        await join_store.load_observed.wait()
        joined_call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await joined_call
        owner_client.release.set()
        result = await owner_call

        self.assertEqual(result.chapters[0].summary, "shared result")
        self.assertEqual(owner_client.call_count, 1)
        self.assertEqual(owner_store.save_count, 1)

    async def test_cancelling_initiating_caller_allows_owner_to_finish_cache(self):
        # Catches caller cancellation destroying the detached owner before a paid result is saved.
        store = ObservedAnalysisStore(self.temporary_directory.name)
        store.save_observed = asyncio.Event()
        client = BlockingStructuredClient(self.one_scene_response())
        analyzer = self.analyzer_with(client, store)

        initiating_call = asyncio.create_task(analyzer.analyze(self.story))
        await client.started.wait()
        initiating_call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await initiating_call
        client.release.set()
        await asyncio.wait_for(store.save_observed.wait(), timeout=1)

        self.assertEqual(client.call_count, 1)
        self.assertEqual(store.save_count, 1)
        self.assertIsNotNone(store.load(self.key))

    async def test_shared_failure_fans_out_once_then_next_call_retries(self):
        # Catches failed owners being duplicated or retained as a permanent poisoned flight.
        owner_store = ObservedAnalysisStore(self.temporary_directory.name)
        join_store = ObservedAnalysisStore(self.temporary_directory.name)
        join_store.load_observed = asyncio.Event()
        failed_client = BlockingStructuredClient(StoryAnalysisError("provider failed"))
        owner = self.analyzer_with(failed_client, owner_store)
        joiner = self.analyzer_with(
            FakeStructuredClient([AssertionError("joiner must not call model")]),
            join_store,
        )

        owner_call = asyncio.create_task(owner.analyze(self.story))
        await failed_client.started.wait()
        joined_call = asyncio.create_task(joiner.analyze(self.story))
        await join_store.load_observed.wait()
        failed_client.release.set()
        failures = await asyncio.gather(owner_call, joined_call, return_exceptions=True)

        self.assertTrue(all(isinstance(item, StoryAnalysisError) for item in failures))
        self.assertIs(failures[0], failures[1])
        self.assertEqual(failed_client.call_count, 1)
        self.assertEqual(owner_store.save_count + join_store.save_count, 0)

        retry_client = FakeStructuredClient([self.one_scene_response("retry succeeded")])
        result = await self.analyzer_with(retry_client, owner_store).analyze(self.story)

        self.assertEqual(retry_client.call_count, 1)
        self.assertEqual(result.chapters[0].summary, "retry succeeded")
        self.assertEqual(owner_store.save_count, 1)

    def test_analyzer_rejects_empty_effective_analysis_identities(self):
        # Catches empty model/prompt/DLC identities collapsing independent cache entries.
        cases = (
            (FakeStructuredClient([], model=""), "faithful-v1", "test-dlc-v1"),
            (FakeStructuredClient([]), " ", "test-dlc-v1"),
            (FakeStructuredClient([]), "faithful-v1", "\t"),
        )
        for client, prompt_version, dlc_version in cases:
            with self.subTest(
                model=client.model,
                prompt_version=prompt_version,
                dlc_version=dlc_version,
            ), self.assertRaises(ValueError):
                StoryAnalyzer(
                    client=client,
                    store=self.store,
                    prompt_version=prompt_version,
                    dlc_version=dlc_version,
                    max_chunk_chars=8,
                )

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
            prompt_version="faithful-v1",
            dlc_version="test-dlc-v1",
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
        client = FakeStructuredClient(
            [
                ContextLimitError("too long"),
                {"scenes": [{"start": 0, "end": 11, "summary": "开端", "pace": 1}]},
                {"scenes": [{"start": 0, "end": 10, "summary": "继续", "pace": 1}]},
            ]
        )
        analyzer = StoryAnalyzer(
            client,
            self.store,
            "faithful-v1",
            "test-dlc-v1",
            max_chunk_chars=3,
        )

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
        client = FakeStructuredClient(
            [
                ContextLimitError("too long"),
                {"scenes": [{"start": 0, "end": 3, "summary": "甲乙丙", "pace": 1}]},
                {"scenes": [{"start": 0, "end": 3, "summary": "丁戊己", "pace": 1}]},
                {"scenes": [{"start": 0, "end": 1, "summary": "庚", "pace": 1}]},
            ]
        )
        analyzer = StoryAnalyzer(
            client,
            self.store,
            "faithful-v1",
            "test-dlc-v1",
            max_chunk_chars=3,
        )

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
        client = FakeStructuredClient(
            [
                ContextLimitError("too long"),
                {"scenes": [{"start": 0, "end": 3, "summary": "abc", "pace": 1}]},
                {"scenes": [{"start": 0, "end": 3, "summary": "d", "pace": 1}]},
                {"scenes": [{"start": 0, "end": 2, "summary": "ef", "pace": 1}]},
            ]
        )
        analyzer = StoryAnalyzer(
            client,
            self.store,
            "faithful-v1",
            "test-dlc-v1",
            max_chunk_chars=3,
        )

        await analyzer.analyze(story)

        chunks = [call["user_content"] for call in client.calls[1:]]
        self.assertEqual(chunks, ["abc", "\n\nd", "ef"])
        self.assertEqual("".join(chunks), story.text)
        self.assertTrue(all(chunk.strip() for chunk in chunks))

    async def test_long_unicode_whitespace_attaches_to_meaningful_chunks_losslessly(self):
        # Catches fixed-width splitting emitting whitespace-only chunks or losing raw offsets.
        story = imported_story(
            ("\u2003" * 4) + "甲乙" + ("\u3000" * 6) + "丙丁" + ("\u2003" * 4)
        )
        client = PartitioningStructuredClient()
        analyzer = StoryAnalyzer(
            client,
            self.store,
            "faithful-v1",
            "test-dlc-v1",
            max_chunk_chars=2,
        )

        result = await analyzer.analyze(story)

        chunks = [call["user_content"] for call in client.calls[1:]]
        self.assertEqual("".join(chunks), story.text)
        self.assertTrue(all(chunk.strip() for chunk in chunks))
        self.assertTrue(any(len(chunk) > 2 for chunk in chunks))
        self.assertEqual(result.text_length, len(story.text))
        self.assertEqual(
            [(chapter.start_offset, chapter.end_offset) for chapter in result.chapters],
            [(0, 12), (12, 18)],
        )

    async def test_whitespace_only_story_is_rejected_without_model_or_cache(self):
        # Catches an impossible all-whitespace partition being sent to the provider.
        story = imported_story("\u2003\u3000\n\t")
        client = FakeStructuredClient([AssertionError("model must not be called")])
        analyzer = StoryAnalyzer(
            client,
            self.store,
            "faithful-v1",
            "test-dlc-v1",
            max_chunk_chars=2,
        )
        key = AnalysisKey(story.source_sha256, "test-model", "faithful-v1", "test-dlc-v1")

        with self.assertRaises(StoryAnalysisError):
            await analyzer.analyze(story)

        self.assertEqual(client.call_count, 0)
        self.assertIsNone(self.store.load(key))

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

        retry_client = FakeStructuredClient([self.one_scene_response("after cancellation")])
        result = await self.analyzer(retry_client).analyze(self.story)
        self.assertEqual(retry_client.call_count, 1)
        self.assertEqual(result.chapters[0].summary, "after cancellation")

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

    async def test_huge_integer_pace_is_a_typed_non_context_analysis_error(self):
        # Catches float conversion overflow escaping the retryable analysis contract.
        client = FakeStructuredClient(
            [
                {
                    "scenes": [
                        {
                            "start": 0,
                            "end": 13,
                            "summary": "scene",
                            "pace": 10**10_000,
                        }
                    ]
                }
            ]
        )

        with self.assertRaises(StoryAnalysisError) as raised:
            await self.analyzer(client).analyze(self.story)

        self.assertNotIsInstance(raised.exception, ContextLimitError)
        self.assertIsNone(self.store.load(self.key))


class StructuredLLMTests(unittest.IsolatedAsyncioTestCase):
    def llm_with_response(self, status_code, payload):
        async def handle(request):
            llm.request_count += 1
            return httpx.Response(status_code, json=payload, request=request)

        llm = object.__new__(LLM)
        llm.url = "https://example.invalid/chat/completions"
        llm.api_key = "test-key"
        llm.model = "test-model"
        llm.temperature = 0.5
        llm.max_tokens = 1000
        llm.reasoning_effort = ""
        llm.request_count = 0
        llm.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(llm.client.aclose)
        return llm

    def llm_with_raw_response(self, status_code, content):
        async def handle(request):
            llm.request_count += 1
            return httpx.Response(status_code, content=content, request=request)

        llm = object.__new__(LLM)
        llm.url = "https://example.invalid/chat/completions"
        llm.api_key = "test-key"
        llm.model = "test-model"
        llm.temperature = 0.5
        llm.max_tokens = 1000
        llm.reasoning_effort = ""
        llm.request_count = 0
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
        identities = (
            {"code": "context_length_exceeded"},
            {"type": "context_window_exceeded"},
            {
                "type": "max_context_length_exceeded",
                "code": "maximum_context_length_exceeded",
            },
        )
        for identity in identities:
            with self.subTest(identity=identity):
                llm = self.llm_with_response(
                    400,
                    {
                        "error": {
                            "message": "Provider rejected the structured request.",
                            **identity,
                        }
                    },
                )

                with self.assertRaises(ContextLimitError):
                    await llm.complete_json("system", "private novel", "story_map")

    async def test_unknown_structured_identity_never_uses_message_inference(self):
        # Catches new provider account/policy identities activating a second request.
        explicit_overage = (
            "This request contains 140000 tokens, exceeding the model context "
            "limit of 128000 tokens."
        )
        cases = (
            ("forbidden-diagnostic", {"type": "forbidden"}, explicit_overage),
            (
                "forbidden-wording",
                {"code": "forbidden"},
                "Request forbidden: input token count 140000 exceeds the model context limit of 128000.",
            ),
            (
                "policy-diagnostic",
                {"code": "policy_violation"},
                explicit_overage,
            ),
            (
                "policy-wording",
                {"type": "policy_violation"},
                "Safety policy violation: prompt token count 140000 exceeds the context window of 128000.",
            ),
            ("throttled-diagnostic", {"type": "throttled"}, explicit_overage),
            (
                "throttled-wording",
                {"code": "throttled"},
                "Requests are being throttled; input token count 140000 exceeds the context limit of 128000.",
            ),
            (
                "usage-diagnostic",
                {"code": "usage_limit_exceeded"},
                explicit_overage,
            ),
            (
                "usage-wording",
                {"type": "usage_limit_exceeded"},
                "Monthly usage limit exceeded; this request has 140000 tokens above the 128000-token context window.",
            ),
            (
                "balance-diagnostic",
                {"type": "account_balance_error"},
                explicit_overage,
            ),
            (
                "balance-wording",
                {"code": "account_balance_error"},
                "Account balance is too low; input token count 140000 exceeds the model context limit of 128000.",
            ),
            ("busy-diagnostic", {"code": "server_busy"}, explicit_overage),
            (
                "busy-wording",
                {"type": "server_busy"},
                "Server is busy; prompt token count 140000 exceeds the model context window of 128000.",
            ),
            (
                "mixed-trusted-and-unknown",
                {
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                },
                explicit_overage,
            ),
        )
        for name, identity, message in cases:
            with self.subTest(name=name):
                llm = self.llm_with_response(
                    400,
                    {"error": {"message": message, **identity}},
                )
                with self.assertRaises(StoryAnalysisError) as raised:
                    await llm.complete_json("system", "private novel", "story_map")
                self.assertNotIsInstance(raised.exception, ContextLimitError)
                self.assertEqual(llm.request_count, 1)

    async def test_untrusted_present_identity_never_uses_message_inference(self):
        # Catches present malformed identities being discarded before message inference.
        explicit_overage = (
            "This request contains 140000 tokens, exceeding the model context "
            "limit of 128000 tokens."
        )
        cases = (
            ("numeric-code", {"code": 400}),
            ("numeric-type", {"type": 1}),
            ("boolean-code", {"code": True}),
            ("object-code", {"code": {"name": "context_length_exceeded"}}),
            ("array-type", {"type": ["context_length_exceeded"]}),
            ("overlong-code", {"code": "x" * 1_001}),
            (
                "trusted-code-untrusted-type",
                {"code": "context_length_exceeded", "type": 1},
            ),
        )
        for name, identity in cases:
            with self.subTest(name=name):
                llm = self.llm_with_response(
                    400,
                    {"error": {"message": explicit_overage, **identity}},
                )

                with self.assertRaises(StoryAnalysisError) as raised:
                    await llm.complete_json("system", "private novel", "story_map")

                self.assertNotIsInstance(raised.exception, ContextLimitError)
                self.assertEqual(llm.request_count, 1)

    async def test_auth_forbidden_and_not_found_never_classify_as_context_limit(self):
        # Catches misleading provider fields activating source chunking/resend.
        cases = (
            (
                401,
                {
                    "error": {
                        "message": "context length exceeded",
                        "type": "invalid_request_error",
                        "code": "context_length_exceeded",
                    }
                },
            ),
            (
                403,
                {"error": {"message": "request exceeds the context window", "code": 403}},
            ),
            (
                404,
                {
                    "error": {
                        "message": "Provider returned error",
                        "code": 404,
                        "metadata": {"raw": "too many tokens for this request"},
                    }
                },
            ),
        )
        for status_code, payload in cases:
            with self.subTest(status_code=status_code):
                llm = self.llm_with_response(status_code, payload)
                with self.assertRaises(StoryAnalysisError) as raised:
                    await llm.complete_json("system", "private novel", "story_map")
                self.assertNotIsInstance(raised.exception, ContextLimitError)
                self.assertEqual(llm.request_count, 1)

    async def test_privacy_and_capacity_descriptions_are_not_context_overage(self):
        # Catches descriptive/policy text being mistaken for request-specific token overflow.
        messages = (
            "For privacy and data-policy reasons this request is unavailable; context length exceeded.",
            "Model capacity description: maximum context length is 128000 tokens.",
        )
        for message in messages:
            with self.subTest(message=message):
                llm = self.llm_with_response(
                    400,
                    {"error": {"message": message, "code": 400}},
                )
                with self.assertRaises(StoryAnalysisError) as raised:
                    await llm.complete_json("system", "private novel", "story_map")
                self.assertNotIsInstance(raised.exception, ContextLimitError)

    async def test_http_400_policy_and_account_errors_override_context_wording(self):
        # Catches policy/account failures causing a second paid request through fallback.
        explicit_overage = (
            "This request contains 140000 tokens, exceeding the model context "
            "limit of 128000 tokens."
        )
        cases = (
            (
                "model-description",
                {
                    "message": "The requested model has a maximum context length of 128000 tokens.",
                    "code": 400,
                },
            ),
            (
                "api-key-tier",
                {
                    "message": (
                        "Your API key is not authorized to submit requests exceeding "
                        "the model context token tier."
                    ),
                    "code": 400,
                },
            ),
            (
                "zero-data-retention",
                {
                    "message": (
                        "Zero data retention is unavailable for prompts exceeding "
                        "32000 tokens."
                    ),
                    "code": 400,
                },
            ),
            (
                "auth-type",
                {
                    "message": explicit_overage,
                    "type": "authentication_error",
                    "code": "context_length_exceeded",
                },
            ),
            (
                "permission-code",
                {
                    "message": explicit_overage,
                    "type": "invalid_request_error",
                    "code": "permission_denied",
                },
            ),
            (
                "rate-limit-code",
                {
                    "message": explicit_overage,
                    "type": "invalid_request_error",
                    "code": "rate_limit_exceeded",
                },
            ),
            (
                "quota-code",
                {
                    "message": explicit_overage,
                    "type": "invalid_request_error",
                    "code": "insufficient_quota",
                },
            ),
            (
                "billing-type",
                {
                    "message": explicit_overage,
                    "type": "billing_error",
                    "code": "context_length_exceeded",
                },
            ),
            (
                "capacity-type",
                {
                    "message": explicit_overage,
                    "type": "capacity_error",
                    "code": "context_length_exceeded",
                },
            ),
            (
                "ambiguous-too-many-tokens",
                {
                    "message": "Too many tokens in the request for this model.",
                    "code": 400,
                },
            ),
            (
                "generic-context-overage",
                {
                    "message": "This request exceeds the maximum context length.",
                    "code": 400,
                },
            ),
            (
                "rate-limit-message",
                {
                    "message": (
                        "Rate limit reached while submitting a request whose input "
                        "tokens exceed the model context limit."
                    ),
                    "code": 400,
                },
            ),
            (
                "quota-message",
                {
                    "message": (
                        "You exceeded your current quota while submitting a prompt "
                        "whose token count exceeds the context window."
                    ),
                    "code": 400,
                },
            ),
            (
                "billing-message",
                {
                    "message": (
                        "Billing is inactive for requests whose input token count "
                        "exceeds the model context limit."
                    ),
                    "code": 400,
                },
            ),
            (
                "capacity-message",
                {
                    "message": (
                        "Provider capacity is unavailable for prompts whose token "
                        "count exceeds the context window."
                    ),
                    "code": 400,
                },
            ),
        )
        for name, error in cases:
            with self.subTest(name=name):
                llm = self.llm_with_response(400, {"error": error})
                with self.assertRaises(StoryAnalysisError) as raised:
                    await llm.complete_json("system", "private novel", "story_map")
                self.assertNotIsInstance(raised.exception, ContextLimitError)
                self.assertEqual(llm.request_count, 1)

    async def test_explicit_current_request_overage_is_context_limit(self):
        # Catches losing legitimate message-only context failures from compatible providers.
        messages = (
            (
                "This request contains 140000 tokens, exceeding the model context "
                "limit of 128000 tokens."
            ),
            (
                "Input size is 140000 tokens and exceeds this model's context "
                "window limit of 128000 tokens."
            ),
            (
                "Prompt token count of 140000 exceeds the model maximum context "
                "length of 128000 tokens."
            ),
            (
                "Maximum context length is 128000 tokens, but you requested "
                "140000 tokens; please reduce the input."
            ),
        )
        for message in messages:
            with self.subTest(message=message):
                llm = self.llm_with_response(
                    400,
                    {"error": {"message": message}},
                )
                with self.assertRaises(ContextLimitError):
                    await llm.complete_json("system", "private novel", "story_map")

        llm = self.llm_with_response(
            400,
            {
                "error": {
                    "message": messages[0],
                    "type": "  ",
                    "code": "",
                }
            },
        )
        with self.assertRaises(ContextLimitError):
            await llm.complete_json("system", "private novel", "story_map")

    async def test_empty_or_malformed_success_and_413_are_typed_non_context_errors(self):
        # Characterizes empty/malformed provider bodies without inventing context fallback.
        for status_code, content in ((200, b""), (413, b""), (413, b"not-json")):
            with self.subTest(status_code=status_code, content=content):
                llm = self.llm_with_raw_response(status_code, content)
                with self.assertRaises(StoryAnalysisError) as raised:
                    await llm.complete_json("system", "private novel", "story_map")
                self.assertNotIsInstance(raised.exception, ContextLimitError)
                self.assertEqual(llm.request_count, 1)

    async def test_oversized_json_integer_is_a_typed_non_context_error(self):
        # Catches Python's integer-string parse limit leaking ValueError.
        content = '{"value":' + ("9" * 5_000) + "}"
        llm = self.llm_with_response(
            200,
            {"choices": [{"message": {"content": content}}]},
        )

        with self.assertRaises(StoryAnalysisError) as raised:
            await llm.complete_json("system", "private novel", "story_map")

        self.assertNotIsInstance(raised.exception, ContextLimitError)

    async def test_deeply_nested_json_is_always_a_typed_non_context_error(self):
        # Catches recursion/depth failures escaping any structured JSON parse boundary.
        nested = "[" * 5_000 + "0" + "]" * 5_000
        provider_nested = "[" * 1_500 + "0" + "]" * 1_500
        clients = (
            ("http-error-body", self.llm_with_raw_response(400, nested.encode("ascii"))),
            ("success-body", self.llm_with_raw_response(200, nested.encode("ascii"))),
            (
                "message-content",
                self.llm_with_response(
                    200,
                    {"choices": [{"message": {"content": nested}}]},
                ),
            ),
            (
                "provider-error",
                self.llm_with_response(
                    400,
                    {
                        "error": {
                            "message": "Provider returned error",
                            "code": 400,
                            "metadata": {"raw": provider_nested},
                        }
                    },
                ),
            ),
        )
        for boundary, llm in clients:
            with self.subTest(boundary=boundary):
                with self.assertRaises(StoryAnalysisError) as raised:
                    await llm.complete_json("system", "private novel", "story_map")
                self.assertNotIsInstance(raised.exception, ContextLimitError)
                self.assertEqual(llm.request_count, 1)

    async def test_closed_client_runtime_error_is_typed_and_non_context(self):
        # Catches a closed/injected transport RuntimeError escaping the LLM error contract.
        llm = self.llm_with_response(200, {})
        await llm.client.aclose()

        with self.assertRaises(StoryAnalysisError) as raised:
            await llm.complete_json("system", "private novel", "story_map")

        self.assertNotIsInstance(raised.exception, ContextLimitError)

    async def test_bounded_openrouter_provider_error_classifies_context_limit(self):
        # Catches missing OpenRouter provider errors without scanning unverified body text.
        llm = self.llm_with_response(
            400,
            {
                "error": {
                    "message": "Provider returned error",
                    "metadata": {
                        "provider_name": "test-provider",
                        "raw": json.dumps(
                            {
                                "error": {
                                    "message": "maximum context length exceeded",
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

    async def test_untrusted_provider_raw_identity_suppresses_raw_message_inference(self):
        # Catches parsed provider identities being ignored by a second raw-text scan.
        explicit_overage = (
            "This request contains 140000 tokens, exceeding the model context "
            "limit of 128000 tokens."
        )
        llm = self.llm_with_response(
            400,
            {
                "error": {
                    "message": "Provider returned error",
                    "metadata": {
                        "raw": json.dumps(
                            {"error": {"message": explicit_overage, "code": 400}}
                        )
                    },
                }
            },
        )

        with self.assertRaises(StoryAnalysisError) as raised:
            await llm.complete_json("system", "private novel", "story_map")

        self.assertNotIsInstance(raised.exception, ContextLimitError)
        self.assertEqual(llm.request_count, 1)

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
