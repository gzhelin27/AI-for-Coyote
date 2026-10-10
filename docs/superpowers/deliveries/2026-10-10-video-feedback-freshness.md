# Video feedback freshness repair — 2026-10-10

## Evidence and scope

The deployed backend logged a video interruption at 10:58:49, position
121136 ms. Channel A advanced from generation 116 to 119, policy epoch became
1, and a strength reduction was pending. The user reported no manual control,
cap change, channel toggle, or overheat warning. Subsequent play attempts reused
the retired session and repeatedly reported an ownership change.

The old event handler discarded the `slots.patch` payload and reconciled the
entire cumulative client cache. A waveform amplitude ignored during playback
remained in that cache. A later B-only or policy-only patch, after A's waveform
expired, could replay the old A value as new feedback and trigger a false
over-cap reduction. A synthetic reproduction through the real relay decoder,
application callback, safety layer, coordinator and video session demonstrates
this mechanism. Historical raw feedback values were not logged, so the exact
original phone event cannot be established retrospectively.

## Change

- Snapshot each incoming slot delta before merging it into the UI cache;
  attach its sender from the relay envelope.
- Reconcile only that delta, and only for the selected client and device slot.
- Preserve partial cap/overheat semantics and existing safety preemption.
- Log channel, fresh reported strength, effective cap, confirmed strength and
  overheat state when feedback triggers a safety reduction. No content, device
  identifiers or credentials are included in the new diagnostic.

The repair does not reclaim retired ownership automatically. A genuine external
safety intervention still stops video output. Classification of a genuinely new
report delayed across a waveform boundary remains based on callback execution
time; that separate timing limitation has not been changed here.

## Verification

- Before the fix, three regressions failed: old A feedback replayed by B-only
  updates, old feedback replayed by policy-only updates, and foreign-slot data
  reaching active output.
- Seven new tests pass, including real over-cap/overheat intervention, sender
  and slot isolation, empty events and nested patch immutability. Removing the
  deep copy makes the queued-overheat regression fail as expected.
- 115 focused video/endpoint tests passed. Independent review found no blocking
  issues and independently ran 80 freshness/endpoint tests successfully.
- Python syntax checks and the frontend clean install/production build passed.
- Full standard Python discovery completed: 1244 tests, 7 skipped, no failures
  or errors (413.577 seconds). The paid LLM probe was not run.
- Isolated browser acceptance played for 182 seconds at normal speed with
  3505 synthetic phone reports, including 1691 partial updates while an old A
  amplitude remained cached. CSV strengths, zero gaps and epoch 1 were preserved.
  An explicit external clear then paused playback, cleared both channels and
  did not resume automatically. No physical frames were sent.

After the user authorized saving the running novel and deploying, its completed
replay archive and idle/zero-output state were verified. The three runtime files
were backed up, copied into the production checkout and verified against the
tested normalized hashes. The backend was restarted and its direct API and
frontend proxy both verified: real-device mode, idle session, A/B output zero,
and per-channel caps 40. The local configuration hash was unchanged. Phone
pairing and real-device playback acceptance remain manual.

Local evidence is retained under ignored `work/oct10-*` paths. Production
configuration, device caps, imported content and CSV files are outside this
change.
