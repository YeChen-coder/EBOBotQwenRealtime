"""Health describes a listening service, including intentional Realtime standby."""
from __future__ import annotations
import math
import threading
import time


class RuntimeState:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = dict(
            architecture='specter-ebo-v2', health_contract_version=3, visual_required=False,
            started_at=time.time(), listener_ready=False, session_state='starting',
            active_user=None, session_id=None, session_trigger=None, session_started_at=0,
            realtime_connected=False, mqtt_connected=False, frigate_ready=False,
            assistant_enabled=True, assistant_control_error='',
            face_library_ready=False, face_library_checked_at=0, profiles=[],
            last_frame_at=0, last_audio_at=0, last_visual_context_at=0,
            visual_context_items_added=0, visual_context_pending=False, visual_last_error='',
            engine_audio_monitor_enabled=False, engine_audio_checked_at=0, engine_audio_health={}, engine_audio_error='',
            engine_video_monitor_enabled=False, engine_video_checked_at=0, engine_video_health={}, engine_video_error='',
            media_stale_after_seconds=20, media_startup_grace_seconds=90,
            media_recovery_attempts=0, last_media_recovery_at=0, last_error='',
            input_transcription_configured=False, input_noise_reduction=None, turn_detection={},
            transcript_path='', last_user_transcript_at=0, user_transcripts_received=0,
            output_audio_dir='', output_audio_files_persisted=0, assistant_transcript_path='',
            assistant_outputs_persisted=0, last_assistant_output_at=0, last_reply_at=0,
            speaker_stream_status='idle', speaker_stream_id='', speaker_stream_played_ms=0,
            speaker_stream_fallbacks=0, speaker_stream_failures=0, barge_in_enabled=False,
            voice_wake_enabled=True, voice_wake_listening=False, voice_wake_transcribing=False,
            voice_wake_candidates=0, voice_wake_accepted=0, voice_wake_rejected=0,
            last_voice_wake_at=0, voice_wake_last_error='', playback_tail_ms=800,
            speech_input_blocked=False,
            barge_in_count=0, last_barge_in_at=0, sessions_completed=0,
            last_barge_in_speech_ms=0, last_barge_in_echo_correlation=0, last_barge_in_residual_ratio=0,
            input_turns_ignored=0, manual_responses_requested=0,
            live_transcription_connected=False, live_transcription_configured=False, live_transcription_last_error='',
            live_transcript_path='', live_user_transcripts_received=0, last_live_user_transcript_at=0,
            response_control='final_transcript_gate', interruption_control='half_duplex',
            memory_pending=False, memory_status='empty', memory_last_error='', memory_last_saved_at=0,
            memory_consolidation_at=0, memory_context_chars=0, tools_in_progress=0,
            aec_enabled=False, input_speech_active=False,
        )

    def update(self, **values):
        with self._lock:
            self._data.update(values)

    def increment(self, key, amount=1):
        with self._lock:
            self._data[key] = self._data.get(key, 0) + amount

    def mark_media(self, kind):
        now = time.time()
        with self._lock:
            self._data['last_frame_at' if kind == 'frame' else 'last_audio_at'] = now
            if all(self._data[k] and now-self._data[k] < self._data['media_stale_after_seconds'] for k in ('last_frame_at','last_audio_at')) and self._data['last_error'].startswith('auto-wake failed:'):
                self._data['last_error'] = ''

    def snapshot(self):
        with self._lock:
            data = dict(self._data)
            data['engine_audio_health'] = dict(data['engine_audio_health'])
            data['engine_video_health'] = dict(data['engine_video_health'])
        now = time.time()
        def age(stamp):
            return max(0, now - stamp) if stamp else None
        frame_age, audio_age = age(data['last_frame_at']), age(data['last_audio_at'])
        video = frame_age is not None and frame_age < data['media_stale_after_seconds'] and data['frigate_ready']
        video_transport = video
        audio = audio_age is not None and audio_age < data['media_stale_after_seconds']
        source_ok, source_status = False, 'not_monitored'
        def fresh(stamp):
            try:
                value = float(stamp or 0)
                return math.isfinite(value) and value > 0 and -5 <= now-value < 20
            except (TypeError, ValueError):
                return False
        video_source = data['engine_video_health']
        source_video_ok, source_video_status = False, 'not_monitored'
        if data['engine_video_monitor_enabled']:
            if data['engine_video_error']:
                source_video_status = 'monitor_error'
            elif not fresh(video_source.get('observed_at')) or not fresh(data['engine_video_checked_at']):
                source_video_status = 'monitor_stale'
            else:
                source_video_status = str(video_source.get('status', 'unknown'))
                source_video_ok = (source_video_status == 'receiving' and video_source.get('source_video_ok') is True
                                   and fresh(video_source.get('last_frame_at')))
                if source_video_status == 'receiving' and not source_video_ok:
                    source_video_status = 'source_stale'
            video = video and source_video_ok
        engine = data['engine_audio_health']
        if data['engine_audio_monitor_enabled']:
            if data['engine_audio_error']:
                source_status = 'monitor_error'
            elif not fresh(engine.get('observed_at')) or not fresh(data['engine_audio_checked_at']):
                source_status = 'monitor_stale'
            else:
                source_status = str(engine.get('status', 'unknown'))
                source_ok = source_status == 'receiving' and engine.get('source_audio_ok') is True
                if source_ok and not all(fresh(engine.get(k)) for k in ('last_packet_at', 'last_pcm_at')):
                    source_ok, source_status = False, 'source_stale'
        # "Played" only means handed to an Agora sender (11-08: robot mute 13 h, logs all fine).
        # The Engine checks that sender belongs to the live, published connection and that Agora
        # reports bytes going out; a fault stays listed for 30 min after it was fixed.
        talk = engine.get('talk') if isinstance(engine.get('talk'), dict) else {}
        speaker_status, speaker_error, speaker_current = 'not_monitored', '', False
        if (data['engine_audio_monitor_enabled'] and not data['engine_audio_error'] and talk
                and fresh(talk.get('observed_at')) and fresh(data['engine_audio_checked_at'])):
            speaker_status = str(talk.get('status', 'unknown'))
            speaker_current = talk.get('ok') is False
            speaker_error = speaker_status if speaker_current else str(talk.get('last_fault') or '')
        starting = now - data['started_at'] < data['media_startup_grace_seconds']
        media_ok = audio and source_ok
        if not data['assistant_enabled']:
            data['session_state'] = 'paused'
            data['voice_wake_listening'] = False
            data['speech_input_blocked'] = True
        session_ok = data['session_state'] in {'standby', 'saving_memory', 'paused'} or (data['session_state'] == 'active' and data['realtime_connected'])
        data.update(
            ok=data['listener_ready'] and media_ok and session_ok and not speaker_current,
            uptime_seconds=round(now-data.pop('started_at'), 1), media_starting=starting,
            realtime_session_started_at=data['session_started_at'],
            realtime_session_age_seconds=age(data['session_started_at']) if data['session_state'] == 'active' else None,
            last_frame_age_seconds=frame_age, last_audio_age_seconds=audio_age,
            video_streaming=video, audio_streaming=audio, media_ok=media_ok,
            audio_ready=media_ok, visual_available=video,
            transport_media_ok=video_transport and audio, source_audio_ok=source_ok, source_audio_status=source_status,
            source_video_ok=source_video_ok, source_video_status=source_video_status,
            speaker_output_status=speaker_status, speaker_output_ok=not speaker_current,
            speaker_output_error=speaker_error, speaker_output_fault_current=speaker_current,
            speaker_output_fault_at=talk.get('last_fault_at') if speaker_error else None,
            speaker_output_fault_healed=bool(talk.get('last_fault_healed')) and not speaker_current,
            speaker_output_sent_bitrate=talk.get('sent_bitrate') if speaker_status != 'not_monitored' else None,
        )
        return data
