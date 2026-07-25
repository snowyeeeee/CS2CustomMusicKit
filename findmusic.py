import json
import os
import random
import sys
import threading

last_played = {}
shuffle_pool = {}
AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg")
_directory_cache = {}

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

_config_cache = None
_last_modified = 0
_config_lock = threading.Lock()


def default_config():
    return {
        "files": {},
        "music_volumes": {},
        "action_durations": {},
        "action_loops": {},
        "fade": True,
        "fade_time": 1,
        "menu_next_on_finish": False,
        "action_loop": True,
        "action_time": 10,
        "combat_music_enabled": True,
        "deathmatch_mode": False,
        "stress_settings": {
            "stress_max": 100,
            "stress_play_threshold": 100,
            "stress_shoot_per_sec": 8,
            "stress_damage": 40,
            "stress_kill": 50,
            "stress_decay_no_shoot": 10,
            "stress_decay_no_damage": 10,
            "hp_survival_low": 78,
            "hp_survival_critical": 77,
        },
    }


def load_config():
    global _config_cache, _last_modified

    try:
        modified = os.path.getmtime(CONFIG_FILE)
    except Exception:
        return default_config()

    with _config_lock:
        if _config_cache is not None and _last_modified == modified:
            return _config_cache

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            return default_config()

        config = default_config()
        if isinstance(loaded, dict):
            config.update(loaded)

        _config_cache = config
        _last_modified = modified
        return _config_cache


def invalidate_config_cache():
    global _config_cache, _last_modified
    with _config_lock:
        _config_cache = None
        _last_modified = 0


def clamp_volume(volume):
    try:
        return max(0, min(100, int(volume)))
    except (TypeError, ValueError):
        return 100


def clamp_action_duration(duration):
    try:
        return max(1, min(600, int(float(duration))))
    except (TypeError, ValueError):
        return 10


def _normalize_music_path(path):
    if not isinstance(path, str):
        return None
    return os.path.normcase(os.path.normpath(path))


def _get_volume_for_music(config, event, music):
    music_volumes = config.get("music_volumes", {}).get(event, {})
    if not music_volumes:
        return 100

    if music in music_volumes:
        return clamp_volume(music_volumes[music])

    normalized_target = _normalize_music_path(music)
    if normalized_target:
        for saved_path, saved_volume in music_volumes.items():
            if _normalize_music_path(saved_path) == normalized_target:
                return clamp_volume(saved_volume)

    return 100


def _get_action_duration_for_music(config, music):
    default_duration = clamp_action_duration(config.get("action_time", 10))
    action_durations = config.get("action_durations", {})
    if not action_durations or not music:
        return default_duration

    if music in action_durations:
        return clamp_action_duration(action_durations[music])

    normalized_target = _normalize_music_path(music)
    if normalized_target:
        for saved_path, saved_duration in action_durations.items():
            if _normalize_music_path(saved_path) == normalized_target:
                return clamp_action_duration(saved_duration)

    return default_duration


def _get_action_loop_for_music(config, music):
    default_loop = bool(config.get("action_loop", True))
    action_loops = config.get("action_loops", {})
    if not action_loops or not music:
        return default_loop

    if music in action_loops:
        return bool(action_loops[music])

    normalized_target = _normalize_music_path(music)
    if normalized_target:
        for saved_path, saved_loop in action_loops.items():
            if _normalize_music_path(saved_path) == normalized_target:
                return bool(saved_loop)

    return default_loop


def invalidate_music_path_cache(path=None):
    if path is None:
        _directory_cache.clear()
        return
    _directory_cache.pop(path, None)


def list_audio_files(directory):
    try:
        modified = os.path.getmtime(directory)
    except OSError:
        _directory_cache.pop(directory, None)
        return []

    cached = _directory_cache.get(directory)
    if cached and cached["modified"] == modified:
        return cached["files"]

    try:
        files = sorted(
            entry.path
            for entry in os.scandir(directory)
            if entry.is_file() and entry.name.lower().endswith(AUDIO_EXTENSIONS)
        )
    except OSError:
        _directory_cache.pop(directory, None)
        return []

    _directory_cache[directory] = {"modified": modified, "files": files}
    return files


def get_event_music_list(files_by_event, event):
    lista = files_by_event.get(event, [])

    if isinstance(lista, list):
        return lista
    if isinstance(lista, str):
        if os.path.isdir(lista):
            return list_audio_files(lista)
        if os.path.isfile(lista):
            return [lista]
    return []


def get_random_music(source, event):
    if isinstance(source, list):
        if not source:
            return None
        return pick_random_without_repeat(source, event)

    if os.path.isfile(source):
        return source

    if os.path.isdir(source):
        files = list_audio_files(source)
        if not files:
            return None
        # Pega apenas o nome para o shuffle
        filenames = [os.path.basename(p) for p in files]
        escolhido = pick_random_without_repeat(filenames, event)
        # Retorna caminho completo
        for full_path in files:
            if os.path.basename(full_path) == escolhido:
                return full_path
    return None


def pick_random_without_repeat(source, event):
    """Escolhe uma música de forma aleatória e evita repetir a anterior para o mesmo evento."""
    if not source:
        return None

    source = list(source)

    if len(source) == 1:
        shuffle_pool.pop(event, None)
        last_played[event] = source[0]
        return source[0]

    previous = last_played.get(event)
    pool = shuffle_pool.get(event)

    if not pool:
        pool = list(source)
        random.shuffle(pool)
        shuffle_pool[event] = pool

    if previous is not None and previous in pool:
        pool = [item for item in pool if item != previous]
        if not pool:
            pool = list(source)
            random.shuffle(pool)
        shuffle_pool[event] = pool

    escolhido = pool.pop(0)

    if previous == escolhido and pool:
        pool.append(escolhido)
        escolhido = pool.pop(0)

    shuffle_pool[event] = pool
    last_played[event] = escolhido
    return escolhido


def get_music(event):
    config = load_config()
    return get_music_from_config(config, event)


def get_music_from_config(config, event):
    lista = config.get("files", {}).get(event)
    if not lista:
        return None
    return get_random_music(lista, event)


def get_volume(event):
    config = load_config()
    return get_volume_from_config(config, event)


def get_volume_from_config(config, event):
    music = last_played.get(event)
    if music:
        return _get_volume_for_music(config, event, music)
    return 100


def get_volume_for_music(config, event, music):
    if not music:
        return 100
    return _get_volume_for_music(config, event, music)


def get_action_duration_for_music(config, music):
    return _get_action_duration_for_music(config, music)


def get_action_loop_for_music(config, music):
    return _get_action_loop_for_music(config, music)


def get_event_duration_for_music(config, event, music, default=10):
    event_durations = config.get("event_durations", {}).get(event, {})
    if not event_durations or not music:
        return max(1, int(default))

    if music in event_durations:
        return max(1, int(event_durations[music]))

    normalized_target = _normalize_music_path(music)
    if normalized_target:
        for saved_path, saved_duration in event_durations.items():
            if _normalize_music_path(saved_path) == normalized_target:
                return max(1, int(saved_duration))

    return max(1, int(default))


def get_music_and_volume(config, event):
    music = get_music_from_config(config, event)
    if not music:
        return None, 100
    return music, get_volume_for_music(config, event, music)


def get_action_loop():
    config = load_config()
    return config.get("action_loop", True)
