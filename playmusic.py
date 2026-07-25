import os
import queue
import threading
import time

import pygame

pygame.mixer.pre_init(44100, -16, 2, 512)
pygame.init()
pygame.mixer.set_num_channels(8)  # Reduzido de 12 para melhor desempenho
pygame.mixer.set_reserved(8)

EFFECT_CHANNEL_IDS = tuple(range(8))
effect_channels = [pygame.mixer.Channel(i) for i in EFFECT_CHANNEL_IDS]
effect_channel_index = 0
active_effect_paths = {}

sound_cache = {}
sound_cache_lock = threading.Lock()
path_cache = {}
path_cache_lock = threading.Lock()
audio_queue = queue.Queue(maxsize=32)
audio_shutdown_event = threading.Event()

current_music_path = None
current_music_volume = 100
current_music_loop = False
current_music_pos = 0.0
current_music_session_id = 0
pending_music_request = None
music_state_lock = threading.Lock()

last_effect_times = {}
EFFECT_COOLDOWN = 0.20
audio_worker_thread = None


def _queue_audio_command(command, payload, priority=False):
    if audio_shutdown_event.is_set():
        return
    try:
        audio_queue.put_nowait((command, payload))
        return
    except queue.Full:
        if not priority:
            return

    kept_commands = []
    try:
        while True:
            queued_command, queued_payload = audio_queue.get_nowait()
            if queued_command != "effect":
                kept_commands.append((queued_command, queued_payload))
            audio_queue.task_done()
    except queue.Empty:
        pass

    reserved_slots = max(0, audio_queue.maxsize - 1)
    for item in kept_commands[-reserved_slots:]:
        try:
            audio_queue.put_nowait(item)
        except queue.Full:
            break

    try:
        audio_queue.put_nowait((command, payload))
    except queue.Full:
        pass


def normalize_volume(volume):
    vol = max(0, min(100, int(volume or 100)))
    return (vol / 100.0) ** 2


def _is_audio_file(path):
    if not path:
        return False
    with path_cache_lock:
        cached = path_cache.get(path)
    if cached is not None:
        return cached
    exists = os.path.isfile(path)
    with path_cache_lock:
        path_cache[path] = exists
    return exists


def _preload_effect(path):
    if not _is_audio_file(path):
        return
    with sound_cache_lock:
        if path in sound_cache:
            return
        try:
            sound_cache[path] = pygame.mixer.Sound(path)
        except Exception:
            pass


def _play_effect_on_reserved_channel(sound, path, volume):
    global effect_channel_index

    for offset in range(len(effect_channels)):
        index = (effect_channel_index + offset) % len(effect_channels)
        channel = effect_channels[index]
        if not channel.get_busy():
            effect_channel_index = (index + 1) % len(effect_channels)
            channel.set_volume(normalize_volume(volume))
            channel.play(sound)
            active_effect_paths[index] = path
            return

    channel = effect_channels[effect_channel_index]
    channel_id = effect_channel_index
    effect_channel_index = (effect_channel_index + 1) % len(effect_channels)
    channel.set_volume(normalize_volume(volume))
    channel.play(sound)
    active_effect_paths[channel_id] = path


def _set_active_effect_volume(path, volume):
    normalized_volume = normalize_volume(volume)
    for index, channel in enumerate(effect_channels):
        if not channel.get_busy():
            active_effect_paths.pop(index, None)
            continue
        if active_effect_paths.get(index) == path:
            channel.set_volume(normalized_volume)


def _stop_active_effect(path):
    for index, channel in enumerate(effect_channels):
        if not channel.get_busy():
            active_effect_paths.pop(index, None)
            continue
        if active_effect_paths.get(index) == path:
            channel.stop()
            active_effect_paths.pop(index, None)


def _audio_worker():
    global current_music_path, current_music_volume
    global current_music_loop, current_music_pos, current_music_session_id
    global pending_music_request

    while not audio_shutdown_event.is_set():
        try:
            command, payload = audio_queue.get(timeout=3.0)
        except queue.Empty:
            continue

        try:
            if command == "shutdown":
                pygame.mixer.stop()
                pygame.mixer.music.stop()
                with music_state_lock:
                    current_music_path = None
                    current_music_loop = False
                    current_music_pos = 0.0
                    current_music_session_id += 1
                    pending_music_request = None
                audio_shutdown_event.set()
                return

            if command == "music":
                path = payload["path"]
                volume = payload["volume"]
                loop = payload.get("loop", False)
                fade_ms = payload.get("fade_ms", 0)
                duration = payload.get("duration")

                if current_music_path == path and current_music_loop == loop and pygame.mixer.music.get_busy():
                    pygame.mixer.music.set_volume(normalize_volume(volume))
                    with music_state_lock:
                        current_music_volume = volume
                        if pending_music_request == (path, loop):
                            pending_music_request = None
                    continue

                with music_state_lock:
                    current_music_session_id += 1
                    session_id = current_music_session_id

                pygame.mixer.music.stop()
                pygame.mixer.music.load(path)
                pygame.mixer.music.set_volume(normalize_volume(volume))
                pygame.mixer.music.play(-1 if loop else 0, fade_ms=fade_ms)

                with music_state_lock:
                    current_music_path = path
                    current_music_volume = volume
                    current_music_loop = loop
                    current_music_pos = 0.0
                    if pending_music_request == (path, loop):
                        pending_music_request = None

                if not loop and duration is not None and duration > 0:
                    threading.Thread(
                        target=_stop_music_after_duration,
                        args=(duration, session_id),
                        daemon=True,
                    ).start()

            elif command == "set_volume":
                volume = payload.get("volume", current_music_volume)
                pygame.mixer.music.set_volume(normalize_volume(volume))
                with music_state_lock:
                    current_music_volume = volume

            elif command == "effect":
                path = payload["path"]
                volume = payload.get("volume", 100)
                now = time.monotonic()

                if now - last_effect_times.get(path, 0.0) < EFFECT_COOLDOWN:
                    continue
                last_effect_times[path] = now

                try:
                    with sound_cache_lock:
                        if path not in sound_cache:
                            sound_cache[path] = pygame.mixer.Sound(path)
                        sound = sound_cache[path]

                    sound.set_volume(normalize_volume(volume))
                    _play_effect_on_reserved_channel(sound, path, volume)
                except Exception:
                    pass

            elif command == "set_effect_volume":
                path = payload.get("path")
                volume = payload.get("volume", 100)
                if path:
                    _set_active_effect_volume(path, volume)

            elif command == "stop_effect":
                path = payload.get("path")
                if path:
                    _stop_active_effect(path)

            elif command == "stop_music":
                with music_state_lock:
                    current_music_session_id += 1
                    current_music_path = None
                    current_music_loop = False
                    current_music_pos = 0.0
                    pending_music_request = None
                pygame.mixer.music.fadeout(600)

            elif command == "stop_effects":
                for channel in effect_channels:
                    channel.stop()
                active_effect_paths.clear()

        except Exception as e:
            print("erro audio:", e)
        finally:
            audio_queue.task_done()


def _stop_music_after_duration(duration, session_id):
    time.sleep(duration)
    if audio_shutdown_event.is_set():
        return
    with music_state_lock:
        should_stop = session_id == current_music_session_id and not current_music_loop
    if should_stop and pygame.mixer.music.get_busy():
        pygame.mixer.music.fadeout(800)

def update_volume(volume):
    pygame.mixer.music.set_volume(normalize_volume(volume))

def tocar_musica(caminho, volume=100, loop=False, duration=None, fade_time=0):
    global pending_music_request

    if audio_shutdown_event.is_set():
        return
    if not _is_audio_file(caminho):
        return
    loop = bool(loop)
    with music_state_lock:
        same_current_music = current_music_path == caminho and current_music_loop == loop
        same_pending_music = pending_music_request == (caminho, loop)

        if same_current_music:
            _queue_audio_command("set_volume", {"volume": volume}, priority=True)
            return

        if same_pending_music:
            return

        pending_music_request = (caminho, loop)

    fade_ms = max(0, int(float(fade_time) * 1000))
    _queue_audio_command(
        "music",
        {
            "path": caminho,
            "volume": volume,
            "loop": loop,
            "fade_ms": fade_ms,
            "duration": float(duration) if duration is not None else None,
        },
        priority=True,
    )


def tocar_efeito(caminho, volume=100):
    if audio_shutdown_event.is_set():
        return
    if not _is_audio_file(caminho):
        return
    _queue_audio_command("effect", {"path": caminho, "volume": volume})

def play_event_sound(event_type, config):
    # O som agora toca de forma assíncrona sem travar o loop
    sound_path = f"sounds/{event_type}.mp3"
    if os.path.exists(sound_path):
        try:
            sound = pygame.mixer.Sound(sound_path)
            sound.set_volume(config.get("volume", 0.5))
            sound.play()
        except Exception:
            pass

def parar_musica():
    _queue_audio_command("stop_music", {}, priority=True)


def parar_efeitos():
    _queue_audio_command("stop_effects", {}, priority=True)


def parar_efeito(caminho):
    _queue_audio_command("stop_effect", {"path": caminho}, priority=True)


def set_volume(volume):
    _queue_audio_command("set_volume", {"volume": volume})


def set_effect_volume(caminho, volume):
    _queue_audio_command(
        "set_effect_volume",
        {"path": caminho, "volume": volume},
        priority=True,
    )


def get_current_music():
    with music_state_lock:
        return current_music_path


def get_music_status():
    with music_state_lock:
        return {
            "path": current_music_path,
            "loop": current_music_loop,
            "pending": pending_music_request is not None,
        }


def is_music_playing():
    return pygame.mixer.music.get_busy()


def preload_effect_sounds(paths):
    if audio_shutdown_event.is_set():
        return

    def worker():
        for path in paths:
            if path:
                _preload_effect(path)

    threading.Thread(target=worker, daemon=True).start()


def shutdown_audio():
    if audio_shutdown_event.is_set():
        return

    try:
        audio_queue.put_nowait(("shutdown", {}))
    except Exception:
        audio_shutdown_event.set()

    if audio_worker_thread is not None:
        audio_worker_thread.join(timeout=1.5)

    try:
        pygame.mixer.quit()
    except Exception:
        pass

    try:
        pygame.quit()
    except Exception:
        pass


audio_worker_thread = threading.Thread(target=_audio_worker, daemon=True)
audio_worker_thread.start()
