import asyncio
from types import SimpleNamespace

import pytest

from newtalk.asr import AsrFinal
from newtalk.audio import SpeechBoundary
from newtalk.context_debug import ContextDebugger
from newtalk.transport.websocket import _dispatch_event
from newtalk.voiceprint import VoicePrintIdentification
from tests.test_transport_runtime import RecordingWebSocket, make_runtime


def test_voice_timing_keeps_asr_and_speaker_wait_separate(tmp_path, monkeypatch):
    async def scenario():
        clock = SimpleNamespace(now=10.0)
        monkeypatch.setattr('newtalk.transport.runtime.perf_counter', lambda: clock.now)
        runtime = make_runtime(RecordingWebSocket())
        debugger = ContextDebugger(enabled=True, path=tmp_path / 'trace.jsonl')
        runtime._chat_service.context_debug = debugger
        release = asyncio.Event()
        started = asyncio.Event()
        joining = asyncio.Event()
        resolve = runtime._resolve_voice_speaker

        async def resolve_after_signal(utterance_id):
            joining.set()
            return await resolve(utterance_id)

        runtime._resolve_voice_speaker = resolve_after_signal

        class Voiceprint:
            async def identify(self, **kwargs):
                started.set()
                await release.wait()
                return VoicePrintIdentification(matched=False, identity_id=None, score=0.1, elapsed_ms=350)

        runtime._voiceprint_client = Voiceprint()
        await runtime.start()
        try:
            await runtime._on_speech_boundary(SpeechBoundary('speech_start', 'u1', 0.9, 100))
            clock.now = 11.0
            await runtime._on_speech_boundary(SpeechBoundary('speech_end', 'u1', 0.1, 1100, 460))
            await runtime._on_utterance_audio('u1', bytes(640))
            clock.now = 11.15
            final = asyncio.create_task(runtime._on_asr_event('u1', AsrFinal('你好')))
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(joining.wait(), timeout=1)
            clock.now = 11.35
            release.set()
            await asyncio.wait_for(final, timeout=1)
            timing = next(e for e in debugger.events_for_device('device-1')
                          if e['event'] == 'voice_input_timing')
            assert timing['utterance_id'] == 'u1'
            assert timing['vad_silence_ms'] == 640
            assert timing['vad_to_asr_final_ms'] == pytest.approx(150)
            assert timing['vad_to_speaker_ready_ms'] == pytest.approx(350)
            assert timing['speaker_join_ms'] == pytest.approx(200)
        finally:
            release.set()
            await runtime.close()
            debugger.close()

    asyncio.run(scenario())


def test_playback_audible_is_recorded_without_provider_wait(tmp_path):
    async def scenario():
        runtime = make_runtime(RecordingWebSocket())
        debugger = ContextDebugger(enabled=True, path=tmp_path / 'trace.jsonl')
        runtime._chat_service.context_debug = debugger
        await runtime.start()
        try:
            await _dispatch_event(None, runtime, event={
                'type': 'playback_audible', 'turn_id': 't1', 'stream_id': 's1',
                'output_clock': 'output_timestamp',
                'metrics': {'speech_end_to_audible_ms': 2100, 'vad_to_audible_ms': 1400,
                            'turn_to_audible_ms': 1200},
            }, session_id='session-1')
            event = debugger.events_for_device('device-1', 't1')[0]
            assert event['event'] == 'playback_audible'
            assert event['metrics']['speech_end_to_audible_ms'] == 2100
            assert event['output_clock'] == 'output_timestamp'
        finally:
            await runtime.close()
            debugger.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, True, '100', 10**1000])
def test_invalid_client_audio_metrics_are_rejected(value):
    async def scenario():
        errors = []

        class Runtime:
            async def send_error(self, **kwargs):
                errors.append(kwargs)

        await _dispatch_event(None, Runtime(), event={
            'type': 'playback_audible', 'turn_id': 't', 'stream_id': 's',
            'output_clock': 'render_clock', 'metrics': {'turn_to_audible_ms': value},
        }, session_id='session')
        assert errors[0]['code'] == 'invalid_playback_metric'

    asyncio.run(scenario())
