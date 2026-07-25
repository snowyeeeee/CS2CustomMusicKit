import os
import time

from findmusic import (
    get_action_duration_for_music,
    get_action_loop_for_music,
    get_event_duration_for_music,
    get_event_music_list,
    get_music_and_volume,
    get_volume_for_music,
    load_config,
)
from playmusic import parar_efeito, parar_efeitos, parar_musica, preload_effect_sounds, tocar_efeito, tocar_musica
from playmusic import get_music_status, is_music_playing

DEBUG = False

EVENT_COOLDOWNS = {
    "kill": 0.0,
    "death": 0.10,
    "bomb_planted": 0.0,
    "bomb_10s": 0.8,
    "round_10s": 0.8,
}

PROCESS_INTERVAL = 0.01
MENU_TRANSITION_GRACE = 0.2
MENU_STABLE_TIME = 0.2
BOMB_MAX_LOCK_TIME = 45.0
DEFAULT_ROUND_SECONDS = 115.0

last_process_time = 0.0
last_had_map_name_monotonic = 0.0
last_action_play = 0.0
last_round_id = 0
action_played_this_round = False
live_phase_started_at = None

estado_atual = None
played_main_states = set()
main_state_priority = 0
last_phase = None
last_player_id = None
last_spectated_id = None
self_steam_id = None
kills_anteriores = None
hp_anterior = 100
last_round_10s = False
last_bomb_10s = False
last_snapshot = None
last_bomb_state = None
bomb_plant_time = 0.0
bomb_music_locked = False
bomb_locked_track = None
last_event_times = {}
kill_detection_armed = False
menu_candidate_since = None

# --- Estados dinâmicos de combate ---
COMBAT_EVENTS = frozenset({
    "combat_intense",
    "survival",
})
COMBAT_TO_SURVIVAL_FADE = 1.0
SHOOTING_GAP_TOLERANCE = 1.5
STRESS_MAX = 100
STRESS_PLAY_THRESHOLD = 100
STRESS_SHOOT_PER_SEC = 8
STRESS_DAMAGE = 40
STRESS_KILL = 50
STRESS_DECAY_NO_SHOOT = 10
STRESS_DECAY_NO_DAMAGE = 10
HP_SURVIVAL_LOW = 78
HP_SURVIVAL_CRITICAL = 77

combat_estado_atual = None
ultimo_combate = 0.0
shooting_active_since = None
last_shot_time = 0.0
last_damage_time = 0.0
last_kill_combat_time = 0.0
last_ammo_by_weapon = {}
last_combat_state_change = 0.0
last_combat_state_exit = {}
stress_level = 0.0
last_stress_update = 0.0
stress_decay_enabled = False


def _reset_match_state():
    global estado_atual, last_phase, last_bomb_state, last_round_10s, last_bomb_10s, last_action_play
    global kills_anteriores, hp_anterior, kill_detection_armed, bomb_plant_time, menu_candidate_since
    global bomb_music_locked, bomb_locked_track, played_main_states, main_state_priority
    global live_phase_started_at
    global combat_estado_atual, ultimo_combate, shooting_active_since, last_shot_time
    global last_damage_time, last_kill_combat_time, last_ammo_by_weapon
    global last_combat_state_change, last_combat_state_exit
    global stress_level, last_stress_update, stress_decay_enabled
    estado_atual = None
    played_main_states = set()
    main_state_priority = 0
    last_action_play = 0.0
    live_phase_started_at = None
    last_phase = None
    last_bomb_state = None
    last_round_10s = False
    last_bomb_10s = False
    bomb_plant_time = 0.0
    bomb_music_locked = False
    bomb_locked_track = None
    kills_anteriores = None
    hp_anterior = 100
    kill_detection_armed = False
    menu_candidate_since = None
    _reset_combat_state()


def _reset_combat_state():
    global combat_estado_atual, ultimo_combate, shooting_active_since, last_shot_time
    global last_damage_time, last_kill_combat_time, last_ammo_by_weapon
    global last_combat_state_change, stress_level, last_stress_update, stress_decay_enabled
    combat_estado_atual = None
    ultimo_combate = 0.0
    shooting_active_since = None
    last_shot_time = 0.0
    last_damage_time = 0.0
    last_kill_combat_time = 0.0
    last_ammo_by_weapon = {}
    last_combat_state_change = 0.0
    stress_level = 0.0
    last_stress_update = 0.0
    stress_decay_enabled = False


def _reset_stress_meter():
    global stress_level, last_stress_update, stress_decay_enabled
    stress_level = 0.0
    last_stress_update = 0.0
    stress_decay_enabled = False


def _halt_combat_music_on_death():
    global combat_estado_atual, estado_atual, last_combat_state_exit
    _reset_stress_meter()
    if not combat_estado_atual:
        return
    if combat_estado_atual in COMBAT_EVENTS:
        last_combat_state_exit[combat_estado_atual] = time.monotonic()
        _log_combat_deactivation(combat_estado_atual, "jogador morto — estresse zerado")
    combat_estado_atual = None
    if estado_atual in COMBAT_EVENTS:
        estado_atual = None
        parar_musica()


def _map_name_loaded(map_data):
    return bool(((map_data or {}).get("name") or "").strip())


def _is_active_player_activity(activity):
    return activity in {"playing", "spectating"}


def is_dm_mode(game_mode):
    return game_mode in ("deathmatch", "gungameprogressive")


def _get_canonical_event(event):
    return {
        "victory": "win_round",
        "defeat": "loose_round",
        "win_round": "win_round",
        "loose_round": "loose_round",
    }.get(event, event)


def _is_real_menu_state(now, last_had_map_name_monotonic, map_data, has_ingame_signals):
    """
    Menu so fora de mapa (sem map.name). Debounce por tempo; nao depende do snapshot.
    O grace usa last_had_map_name_monotonic: atualizado em *todo* tick em que ha map.name,
    para refletir corretamente o instante em que o cliente saiu do mapa (ex.: loading).
    """
    global menu_candidate_since

    if _map_name_loaded(map_data):
        menu_candidate_since = None
        return False

    # Se ainda existem sinais claros de partida ativa, nunca trata como menu.
    if has_ingame_signals:
        menu_candidate_since = None
        return False

    if (now - last_had_map_name_monotonic) <= MENU_TRANSITION_GRACE:
        menu_candidate_since = None
        return False

    if menu_candidate_since is None:
        menu_candidate_since = now
        return False

    return (now - menu_candidate_since) >= MENU_STABLE_TIME


def _debug_log(*args):
    if DEBUG:
        print(*args)


def _parse_float(value):
    try:
        return float(value)
    except Exception:
        return None


def _get_player_team(payload, player, provider_steamid):
    player_team = player.get("team")
    if player_team in ("CT", "T"):
        return player_team

    all_players = payload.get("allplayers") or {}
    if provider_steamid and isinstance(all_players, dict):
        local_player = all_players.get(provider_steamid) or all_players.get(str(provider_steamid))
        if isinstance(local_player, dict):
            player_team = local_player.get("team")
            if player_team in ("CT", "T"):
                return player_team

    return None


def _get_round_win_team(round_data, bomb_state, bomb_countdown_finished):
    win_team = round_data.get("win_team")
    if win_team in ("CT", "T"):
        return win_team
    if bomb_state == "defused":
        return "CT"
    if bomb_state == "exploded" or bomb_countdown_finished:
        return "T"
    return None


ROUND_RESULT_EVENTS = {"win_round", "loose_round"}
MAIN_ROUND_STATES = {"freezetime", "action", "bomb_planted", "bomb_10s", "win_round", "loose_round"}
MAIN_STATE_PRIORITY = {
    "freezetime": 1,
    "action": 2,
    "bomb_planted": 3,
    "bomb_10s": 4,
    "win_round": 5,
    "loose_round": 5,
}


def _main_state_already_played(event):
    return event in MAIN_ROUND_STATES and event in played_main_states


def _mark_main_state_played(event):
    global main_state_priority
    if event in MAIN_ROUND_STATES:
        played_main_states.add(event)
        main_state_priority = max(main_state_priority, MAIN_STATE_PRIORITY[event])


def _can_play_main_state(event):
    if event in ROUND_RESULT_EVENTS:
        return True  # SEMPRE pode tocar

    if event not in MAIN_ROUND_STATES:
        return True

    if _main_state_already_played(event):
        return False

    return MAIN_STATE_PRIORITY[event] > main_state_priority


def _bomb_audio_active(bomb_state=None):
    return (
        bomb_state == "planted"
        or bomb_music_locked
        or estado_atual in ("bomb_planted", "bomb_10s")
    )


def _stop_music_if_no_bomb(bomb_state=None):
    if _bomb_audio_active(bomb_state):
        return False
    parar_musica()
    return True


def _bomb_lock_timed_out(now):
    return (
        bomb_music_locked
        and bomb_plant_time > 0.0
        and (now - bomb_plant_time) >= BOMB_MAX_LOCK_TIME
    )


def _finish_bomb_music(config=None, play_end_round=True, round_result_event="loose_round"):
    global bomb_plant_time, estado_atual, bomb_music_locked, bomb_locked_track
    global last_bomb_state, last_bomb_10s

    parar_musica()
    bomb_plant_time = 0.0
    estado_atual = None
    bomb_music_locked = False
    bomb_locked_track = None
    last_bomb_state = None
    last_bomb_10s = False

    if play_end_round and config is not None:
        _play_state(round_result_event, config)


def _is_deathmatch_mode_enabled(config, game_mode=None):
    if is_dm_mode(game_mode):
        return True
    return bool(config.get("deathmatch_mode", False))


def _is_event_allowed_in_deathmatch_mode(event, config, game_mode=None):
    if not _is_deathmatch_mode_enabled(config, game_mode):
        return True

    canonical_event = _get_canonical_event(event)
    allowed_events = {"menu", "kill", "death", "win_round", "loose_round"}
    if config.get("combat_music_enabled", True):
        allowed_events.update({"combat_intense", "survival"})

    return canonical_event in allowed_events


def _play_state(event, config, game_mode=None):
    global bomb_music_locked, estado_atual
    canonical_event = _get_canonical_event(event)
    if not _can_play_main_state(canonical_event):
        return
    if canonical_event == estado_atual:
        return
    if not _is_event_allowed_in_deathmatch_mode(canonical_event, config, game_mode):
        parar_musica()
        estado_atual = None
        return

    if canonical_event in ROUND_RESULT_EVENTS or canonical_event == "menu":
        parar_efeitos()

    music, vol = get_music_and_volume(config, canonical_event)
    if bomb_music_locked and canonical_event not in ROUND_RESULT_EVENTS and canonical_event != "menu":
        return
    if music:
        estado_atual = canonical_event
        _mark_main_state_played(canonical_event)
        # Fim de round deve tocar uma vez so por evento.
        should_loop = canonical_event not in ROUND_RESULT_EVENTS
        if canonical_event == "menu":
            should_loop = not bool(config.get("menu_next_on_finish", False))
        tocar_musica(music, vol, loop=should_loop)
    if bomb_music_locked and estado_atual == "bomb":
        return


def _play_next_menu_music(config):
    global estado_atual
    music, vol = get_music_and_volume(config, "menu")
    if music:
        estado_atual = "menu"
        tocar_musica(music, vol, loop=False)


def _sync_menu_music_mode(config):
    status = get_music_status()
    current_music = status.get("path")
    if not current_music or status.get("pending"):
        return

    next_on_finish = bool(config.get("menu_next_on_finish", False))
    current_loop = bool(status.get("loop"))

    if next_on_finish:
        if current_loop:
            volume = get_volume_for_music(config, "menu", current_music)
            tocar_musica(current_music, volume, loop=False)
            return
        if not is_music_playing():
            _play_next_menu_music(config)
    elif not current_loop:
        volume = get_volume_for_music(config, "menu", current_music)
        tocar_musica(current_music, volume, loop=True)


def _play_event(config, event, game_mode=None):
    if not _is_event_allowed_in_deathmatch_mode(event, config, game_mode):
        return

    now = time.monotonic()
    cd = EVENT_COOLDOWNS.get(event, 0.0)
    if cd and now - last_event_times.get(event, 0) < cd:
        return
    music, vol = get_music_and_volume(config, event)
    if music:
        last_event_times[event] = now
        tocar_efeito(music, vol)


def _stop_event_effects(config, event):
    for music in get_event_music_list(config.get("files", {}), event):
        parar_efeito(music)


def _play_action_music(config, game_mode=None):
    global estado_atual, action_played_this_round

    if not _is_event_allowed_in_deathmatch_mode("action", config, game_mode):
        return

    if action_played_this_round:
        return

    if not _can_play_main_state("action"):
        action_played_this_round = True
        return

    if bomb_music_locked or estado_atual in ("bomb_planted", "bomb_10s"):
        return

    music, vol = get_music_and_volume(config, "action")
    if not music:
        return

    loop = get_action_loop_for_music(config, music)
    duration = None if loop else get_action_duration_for_music(config, music)
    fade = config.get("fade_time", 1) if config.get("fade", True) else 0

    estado_atual = "action"
    action_played_this_round = True
    _mark_main_state_played("action")

    tocar_musica(music, vol, loop=loop, duration=duration, fade_time=fade)

    if DEBUG:
        print(">>> ACTION BLOQUEADA PARA ESTE ROUND")


def _ensure_effects_preloaded(config):
    files = config.get("files", {})
    sig = tuple(
        (e, tuple(get_event_music_list(files, e)))
        for e in ("kill", "death", "bomb_planted", "round_10s", "bomb_10s")
    )
    if sig == getattr(_ensure_effects_preloaded, "sig", None):
        return
    paths = [p for _, fs in sig for p in fs]
    preload_effect_sounds(paths)
    _ensure_effects_preloaded.sig = sig


def _get_active_weapon(player):
    weapons = player.get("weapons") or {}
    if not isinstance(weapons, dict):
        return None, None

    for slot, weapon in weapons.items():
        if not isinstance(weapon, dict):
            continue
        if weapon.get("state") == "active":
            return slot, weapon
    return None, None


def _track_shooting(player, now):
    global shooting_active_since, last_shot_time, last_ammo_by_weapon, ultimo_combate

    _, active_weapon = _get_active_weapon(player)
    if not active_weapon:
        return False, None

    weapon_name = active_weapon.get("name") or "unknown"
    ammo_clip = active_weapon.get("ammo_clip")
    shot_fired = False

    if ammo_clip is not None:
        prev_ammo = last_ammo_by_weapon.get(weapon_name)
        if prev_ammo is not None and ammo_clip < prev_ammo:
            shot_fired = True
        last_ammo_by_weapon[weapon_name] = ammo_clip

    if shot_fired:
        if shooting_active_since is None or (now - last_shot_time) > SHOOTING_GAP_TOLERANCE:
            shooting_active_since = now
        last_shot_time = now
        ultimo_combate = now

    return shot_fired, active_weapon


def _track_damage(hp, now):
    global last_damage_time, hp_anterior, ultimo_combate

    if hp is None:
        return False

    damage_taken = False
    if hp_anterior is not None and hp < hp_anterior and hp > 0:
        damage_taken = True
        last_damage_time = now
        ultimo_combate = now

    return damage_taken


def _track_kill_for_combat(kills, kills_anteriores_val, now):
    global last_kill_combat_time, ultimo_combate

    if kills is None or kills_anteriores_val is None:
        return False

    if kills > kills_anteriores_val:
        last_kill_combat_time = now
        ultimo_combate = now
        return True
    return False


def _update_stress(now, damage_taken, kill_scored):
    global stress_level, last_stress_update, stress_decay_enabled
    global last_shot_time, last_damage_time

    if last_stress_update <= 0:
        dt = 0.0
    else:
        dt = min(now - last_stress_update, 0.25)
    last_stress_update = now

    was_decay_enabled = stress_decay_enabled

    is_shooting = last_shot_time > 0 and (now - last_shot_time) <= SHOOTING_GAP_TOLERANCE
    if dt > 0 and is_shooting:
        stress_level = min(STRESS_MAX, stress_level + STRESS_SHOOT_PER_SEC * dt)
    if damage_taken:
        stress_level = min(STRESS_MAX, stress_level + STRESS_DAMAGE)
    if kill_scored:
        stress_level = min(STRESS_MAX, stress_level + STRESS_KILL)

    if stress_level >= STRESS_PLAY_THRESHOLD:
        stress_decay_enabled = True

    if was_decay_enabled and dt > 0:
        if last_shot_time <= 0 or (now - last_shot_time) > SHOOTING_GAP_TOLERANCE:
            stress_level = max(0.0, stress_level - STRESS_DECAY_NO_SHOOT * dt)
        if last_damage_time <= 0 or (now - last_damage_time) > SHOOTING_GAP_TOLERANCE:
            stress_level = max(0.0, stress_level - STRESS_DECAY_NO_DAMAGE * dt)

    stress_level = min(STRESS_MAX, max(0.0, stress_level))

    if stress_decay_enabled and stress_level <= 0:
        stress_level = 0.0
        stress_decay_enabled = False


def _survival_state_for_hp(hp):
    if hp is None or hp <= 0:
        return None
    if hp < HP_SURVIVAL_CRITICAL:
        return "survival_critical"
    if hp < HP_SURVIVAL_LOW:
        return "survival_low"
    return None


def _log_combat_activation(state, reasons, gsi_context):
    labels = {
        "combat_intense": "COMBATE INTENSO",
        "survival": "SOBREVIVENCIA",
    }
    print(f">>> {labels.get(state, state.upper())} ATIVADO")
    print("  Motivos:")
    for reason in reasons:
        print(f"    - {reason}")
    print("  Dados GSI utilizados:")
    for key, value in gsi_context.items():
        print(f"    - {key}: {value}")


def _log_combat_deactivation(state, reason):
    print(f">>> {state.upper()} DESATIVADO: {reason}")


def _play_combat_music(state, config, reasons, gsi_context, loop=True, duration=None, fade_time=None, game_mode=None):
    global combat_estado_atual, estado_atual, last_combat_state_change, last_combat_state_exit

    if not _is_event_allowed_in_deathmatch_mode(state, config, game_mode):
        return

    if state == combat_estado_atual:
        return

    music, vol = get_music_and_volume(config, state)
    if not music:
        return

    if fade_time is not None:
        fade = fade_time
    else:
        fade = config.get("fade_time", 1) if config.get("fade", True) else 0

    if combat_estado_atual and combat_estado_atual in COMBAT_EVENTS:
        last_combat_state_exit[combat_estado_atual] = time.monotonic()
        _log_combat_deactivation(combat_estado_atual, f"troca para {state}")

    _log_combat_activation(state, reasons, gsi_context)

    combat_estado_atual = state
    estado_atual = state
    last_combat_state_change = time.monotonic()
    tocar_musica(music, vol, loop=loop, duration=duration, fade_time=fade)


def _stop_combat_music(reason):
    global combat_estado_atual, estado_atual, last_combat_state_exit

    if not combat_estado_atual:
        return

    if combat_estado_atual in COMBAT_EVENTS:
        last_combat_state_exit[combat_estado_atual] = time.monotonic()
        _log_combat_deactivation(combat_estado_atual, reason)

    combat_estado_atual = None
    if estado_atual in COMBAT_EVENTS:
        estado_atual = None
        parar_musica()


def _transition_combat_to_survival(config, hp, now, gsi_context, game_mode=None):
    survival_tier = _survival_state_for_hp(hp)
    if not survival_tier or not hp or hp <= 0:
        _stop_combat_music("combate encerrado sem condicoes de sobrevivencia")
        return

    hp_label = "critico" if survival_tier == "survival_critical" else "baixo"
    reasons = [
        f"medidor de estresse esvaziou (0/{STRESS_MAX})",
        f"transicao com fade de {COMBAT_TO_SURVIVAL_FADE}s para sobrevivencia",
        f"HP {hp_label}: {hp}",
        "jogador continua vivo",
    ]
    music, _ = get_music_and_volume(config, "survival")
    duration = get_event_duration_for_music(config, "survival", music) if music else None
    _play_combat_music(
        "survival",
        config,
        reasons,
        gsi_context,
        loop=False,
        duration=duration,
        fade_time=COMBAT_TO_SURVIVAL_FADE,
        game_mode=game_mode,
    )


def _evaluate_combat_music(player, hp, kills, kills_anteriores_val, now, config, game_mode=None):
    global combat_estado_atual

    if bomb_music_locked or estado_atual in ("bomb_planted", "bomb_10s"):
        return

    player_state = player.get("state") or {}
    gsi_health = player_state.get("health", hp)
    match_stats = player.get("match_stats") or {}
    gsi_kills = match_stats.get("kills", kills)

    _, active_weapon = _track_shooting(player, now)
    damage_taken = _track_damage(hp, now)
    kill_scored = _track_kill_for_combat(kills, kills_anteriores_val, now)
    _update_stress(now, damage_taken, kill_scored)

    weapon_name = (active_weapon or {}).get("name")
    weapon_slot, _ = _get_active_weapon(player)
    is_shooting = last_shot_time > 0 and (now - last_shot_time) <= SHOOTING_GAP_TOLERANCE

    gsi_context = {
        "player.state.health": gsi_health,
        "player.match_stats.kills": gsi_kills,
        "arma_ativa": weapon_name or "nenhuma",
        "slot_arma_ativa": weapon_slot or "nenhum",
        "estresse": round(stress_level, 1),
        "estresse_limite": STRESS_PLAY_THRESHOLD,
        "estresse_pode_esvaziar": stress_decay_enabled,
        "atirando": is_shooting,
        "ultimo_tiro_s_atras": round(now - last_shot_time, 1) if last_shot_time else None,
        "ultimo_dano_s_atras": round(now - last_damage_time, 1) if last_damage_time else None,
    }

    if combat_estado_atual == "combat_intense" and stress_level <= 0:
        _transition_combat_to_survival(config, hp, now, gsi_context, game_mode=game_mode)
        return

    if stress_level >= STRESS_PLAY_THRESHOLD and combat_estado_atual != "combat_intense":
        reasons = [f"medidor de estresse atingiu {stress_level:.0f}/{STRESS_PLAY_THRESHOLD}"]
        if is_shooting:
            reasons.append(f"atirando +{STRESS_SHOOT_PER_SEC}/s estresse")
        if damage_taken:
            reasons.append(f"dano recebido +{STRESS_DAMAGE} estresse")
        if kill_scored:
            reasons.append(f"kill +{STRESS_KILL} estresse")
        _play_combat_music(
            "combat_intense",
            config,
            reasons,
            gsi_context,
            loop=True,
            game_mode=game_mode,
        )
        return


def _process_combat_music(data, now, config, phase, is_spectating_other, hp, kills, game_mode=None):
    if _is_deathmatch_mode_enabled(config, game_mode) and not _is_event_allowed_in_deathmatch_mode("combat_intense", config, game_mode):
        if combat_estado_atual:
            _stop_combat_music("modo deathmatch ativo")
        return

    if phase != "live" or is_spectating_other:
        if combat_estado_atual:
            _stop_combat_music("fora da fase live ou espectando")
        return

    if hp is not None and hp <= 0:
        _halt_combat_music_on_death()
        return

    player = data.get("player") or {}
    _evaluate_combat_music(player, hp, kills, kills_anteriores, now, config, game_mode=game_mode)


def detectar(data):
    global kills_anteriores, hp_anterior, last_round_10s, last_bomb_10s
    global last_phase, last_snapshot, last_process_time, last_bomb_state, last_player_id
    global kill_detection_armed, estado_atual, last_spectated_id, bomb_plant_time
    global last_had_map_name_monotonic, self_steam_id
    global last_round_id, action_played_this_round, bomb_music_locked, bomb_locked_track, played_main_states, main_state_priority
    global live_phase_started_at

    now = time.monotonic()
    if now - last_process_time < PROCESS_INTERVAL:
        return
    last_process_time = now

    map_data = data.get("map") or {}
    player = data.get("player") or {}
    provider = data.get("provider") or {}
    round_data = data.get("round") or {}
    bomb_data = data.get("bomb") or {}
    phase_countdowns = data.get("phase_countdowns") or {}

    activity = player.get("activity")
    game_mode = (map_data.get("mode") or "").strip() or None
    dm_mode = is_dm_mode(game_mode)
    phase = phase_countdowns.get("phase") or round_data.get("phase")
    round_bomb_state = round_data.get("bomb")
    payload_bomb_state = bomb_data.get("state")
    phase_ends_in = phase_countdowns.get("phase_ends_in")
    round_time_left = _parse_float(phase_ends_in)
    phase_countdown_available = round_time_left is not None
    round_time_warning_source = None
    estimated_round_time_left = None

    if phase == "live" and live_phase_started_at is None:
        live_phase_started_at = now

    if phase in ("freezetime", "over"):
        live_phase_started_at = None

    round_time_warning_active = False
    if phase == "live" and round_time_left is not None:
        round_time_warning_active = 0 <= round_time_left <= 20
        round_time_warning_source = "phase_countdowns.phase_ends_in"
    elif phase == "live" and live_phase_started_at is not None:
        elapsed_live_time = now - live_phase_started_at
        estimated_round_time_left = DEFAULT_ROUND_SECONDS - elapsed_live_time
        round_time_warning_active = 0 <= estimated_round_time_left <= 20
        round_time_warning_source = "fallback_tempo_local"

    print("Fase:", phase)
    print("Tempo restante:", phase_ends_in)
    print("phase_countdowns disponivel:", bool(phase_countdowns))
    print("Fonte tempo round:", round_time_warning_source)
    if estimated_round_time_left is not None:
        print("Tempo restante estimado:", round(estimated_round_time_left, 1))

    kills = (player.get("match_stats") or {}).get("kills")
    hp = (player.get("state") or {}).get("health")
    bomb_countdown = None
    try:
        bomb_countdown = float(bomb_data.get("countdown"))
    except (TypeError, ValueError):
        bomb_countdown = None
    bomb_countdown_bucket = int(bomb_countdown) if bomb_countdown is not None else None
    bomb_state = round_bomb_state or payload_bomb_state
    if not bomb_state and bomb_countdown is not None and bomb_countdown > 0:
        bomb_state = "planted"
    if bomb_state == "planted" and round_time_warning_active:
        print("Round tempo acabando ignorado: bomba plantada")
        round_time_warning_active = False
        round_time_warning_source = None
    provider_steamid = provider.get("steamid")
    player_team = _get_player_team(data, player, provider_steamid)
    bomb_countdown_finished = bomb_countdown is not None and bomb_countdown <= 0
    win_team = _get_round_win_team(round_data, bomb_state, bomb_countdown_finished)
    venceu = bool(player_team and win_team and win_team == player_team)
    round_result_event = "win_round" if venceu else "loose_round"
    if provider_steamid:
        self_steam_id = provider_steamid

    current_spectated_id = player.get("steamid")
    is_spectating_other = bool(self_steam_id and current_spectated_id and current_spectated_id != self_steam_id)
    has_ingame_signals = bool(
        phase
        or bomb_state
        or _is_active_player_activity(activity)
    )
    has_map = _map_name_loaded(map_data)
    if has_map:
        last_had_map_name_monotonic = now

    if has_map and estado_atual == "menu":
        _stop_music_if_no_bomb(bomb_state)
        estado_atual = None

    if current_spectated_id != last_spectated_id:
            kills_anteriores = kills
            last_spectated_id = current_spectated_id

    was_known_player = False
    _saved_phase = None
    _saved_bomb_state = None
    _saved_bomb_plant_time = 0.0
    _saved_estado = None
    _saved_played_main_states = None
    _saved_main_state_priority = 0
    _saved_round_10s = False
    _saved_bomb_10s = False
    _saved_bomb_music_locked = False
    _saved_bomb_locked_track = None

    current_player_id = self_steam_id or player.get("steamid")
    if current_player_id and current_player_id != last_player_id:
        was_known_player = last_player_id is not None
        if was_known_player:
            _saved_phase = last_phase
            _saved_bomb_state = last_bomb_state
            _saved_bomb_plant_time = bomb_plant_time
            _saved_estado = estado_atual
            _saved_played_main_states = set(played_main_states)
            _saved_main_state_priority = main_state_priority
            _saved_round_10s = last_round_10s
            _saved_bomb_10s = last_bomb_10s
            _saved_bomb_music_locked = bomb_music_locked
            _saved_bomb_locked_track = bomb_locked_track
        _reset_match_state()
        last_player_id = current_player_id
        last_snapshot = None
        # Troca de pawn (ex.: assumir bot): nao re-disparar live/action nem bomba/round aux.
        if was_known_player:
            if _saved_phase:
                last_phase = _saved_phase
            if _saved_bomb_state is not None:
                last_bomb_state = _saved_bomb_state
            bomb_plant_time = _saved_bomb_plant_time
            estado_atual = _saved_estado
            played_main_states = _saved_played_main_states or set()
            main_state_priority = _saved_main_state_priority
            last_round_10s = _saved_round_10s
            last_bomb_10s = _saved_bomb_10s
            bomb_music_locked = _saved_bomb_music_locked
            bomb_locked_track = _saved_bomb_locked_track

    config = None

    def get_config():
        nonlocal config
        if config is None:
            config = load_config()
            _ensure_effects_preloaded(config)
        return config

    # Menu precisa ser avaliado a cada tick (debounce por tempo), nao apenas quando o
    # snapshot muda — senao, com payload estavel no menu, o return abaixo bloqueia para sempre.
    if _is_real_menu_state(now, last_had_map_name_monotonic, map_data, has_ingame_signals):
        menu_config = get_config()
        if estado_atual != "menu":
            if _bomb_audio_active(bomb_state):
                _finish_bomb_music(play_end_round=False)
            parar_musica()
            _reset_match_state()
            _play_state("menu", menu_config)
        else:
            _sync_menu_music_mode(menu_config)
        return

    if bomb_music_locked and (bomb_countdown_finished or _bomb_lock_timed_out(now)):
        print(">>> BOMBA FINALIZADA POR TIMEOUT")
        _finish_bomb_music(get_config())
        return

    # Garantir que o detector de kill fique armado sempre que o jogador estiver
    # em live, tiver stats de kills e nao estiver espectando outro jogador.
    kill_detection_armed = bool(
        phase == "live" and not is_spectating_other and kills is not None
    )

    # Estados de combate dependem de tempo; processar a cada payload GSI.
    _process_combat_music(data, now, get_config(), phase, is_spectating_other, hp, kills)

    snapshot = (
        activity,
        phase,
        bomb_state,
        phase_countdown_available,
        round_time_warning_active,
        kills,
        hp,
        bomb_countdown_bucket,
    )
    if snapshot == last_snapshot:
        return
    last_snapshot = snapshot

    if phase and phase != last_phase:
        if dm_mode:
            last_round_10s = False
            _reset_combat_state()
            if phase == "over":
                print(
                    f">>> ROUND OVER DM: player_team={player_team or 'desconhecido'} "
                    f"win_team={win_team or 'desconhecido'} venceu={venceu} "
                    f"evento={round_result_event}"
                )
                kill_detection_armed = False
                result_event = "victory" if venceu else "defeat"
                _play_state(result_event, get_config(), game_mode=game_mode)
            last_phase = phase
        else:
            if phase == "freezetime":
                last_round_10s = False
                _reset_combat_state()
                if _bomb_audio_active(bomb_state):
                    _finish_bomb_music(play_end_round=False)
                is_new_round = last_phase in (None, "over")
                if is_new_round:
                    last_round_id += 1
                    action_played_this_round = False
                    played_main_states = set()
                    main_state_priority = 0
                if not is_spectating_other:
                    if kills is not None:
                        kills_anteriores = kills
                    kill_detection_armed = False
                    _play_state("freezetime", get_config())
                else:
                    kill_detection_armed = False

            elif phase == "live":
                if kills is not None:
                    kills_anteriores = kills

                kill_detection_armed = bool(kills is not None and not is_spectating_other)
                last_event_times.clear()
            
                if not is_spectating_other:
                    _play_action_music(get_config())

            elif phase == "over":
                last_round_10s = False
                _reset_combat_state()
                print(
                    f">>> ROUND OVER: player_team={player_team or 'desconhecido'} "
                    f"win_team={win_team or 'desconhecido'} venceu={venceu} "
                    f"evento={round_result_event}"
                )

                kill_detection_armed = False
                if _bomb_audio_active(bomb_state):
                    _finish_bomb_music(get_config(), round_result_event=round_result_event)
                elif not _bomb_audio_active(bomb_state):
                    bomb_music_locked = False
                    parar_musica()
                    estado_atual = None
                last_action_play = 0.0
                if not _bomb_audio_active(bomb_state):
                    _play_state(round_result_event, get_config())

            last_phase = phase

    if not dm_mode and bomb_state == "planted" and phase not in ("over", "freezetime"):
        _stop_event_effects(get_config(), "round_10s")

        # Enquanto a bomba estiver travada, nunca troca a trilha atual.
        if bomb_music_locked and bomb_locked_track:
            if _bomb_lock_timed_out(now):
                print(">>> BOMBA FINALIZADA POR TIMEOUT")
                _finish_bomb_music(get_config())
                return
            last_bomb_state = "planted"
            if estado_atual != "bomb_planted":
                estado_atual = "bomb_planted"
    
        bomb_already_active = (
            last_bomb_state == "planted"
            or bomb_music_locked
            or estado_atual in ("bomb_planted", "bomb_10s")
            or _main_state_already_played("bomb_planted")
        )

        if bomb_plant_time <= 0.0:
            bomb_plant_time = now

        last_bomb_state = "planted"
    
        if not bomb_already_active and not bomb_music_locked and _can_play_main_state("bomb_planted"):
            musica = get_music_and_volume(get_config(), "bomb_planted")
    
            if musica[0]:
                tocar_musica(musica[0], musica[1], loop=True)
    
                estado_atual = "bomb_planted"
                bomb_music_locked = True
                bomb_locked_track = musica[0]
    
                _mark_main_state_played("bomb_planted")
    
                print(f">>> BOMBA GARANTIDA: {os.path.basename(musica[0])}")
    
        # fallback de tempo
        if bomb_plant_time <= 0.0:
            bomb_plant_time = now
    
        # ✅ CORRETO (sem vírgula)
        last_bomb_state = "planted"

    elif not dm_mode and bomb_state in ["exploded", "defused"]:
        if last_bomb_state == "planted":
            parar_musica()
            bomb_plant_time = 0.0
            estado_atual = None
            bomb_music_locked = False
            bomb_locked_track = None
    
            # 🔥 FORÇA END ROUND
            _play_state(round_result_event, get_config())
    
            print(f">>> BOMBA FINALIZADA ({bomb_state})")
    
        last_bomb_state = bomb_state

    if not dm_mode and phase == "over" and estado_atual in ["bomb_planted", "bomb_10s"]:
        print(">>> ROUND OVER: musica da bomba aguardando resultado do round.")

    if not dm_mode and round_time_warning_active:
        if not last_round_10s:
            print(f">>> ROUND TEMPO ACABANDO: fonte={round_time_warning_source}")
            _play_event(get_config(), "round_10s")
            last_round_10s = True
    elif phase in ("freezetime", "over"):
        last_round_10s = False

    should_trigger_bomb_10s = False
    if not dm_mode and bomb_state == "planted" and phase == "live" and not last_bomb_10s:
        # Usa o countdown real da bomba quando disponivel para maior precisao.
        if bomb_countdown is not None:
            should_trigger_bomb_10s = 0 < bomb_countdown <= 10.1
        elif bomb_plant_time > 0:
            tempo_decorrido = now - bomb_plant_time
            should_trigger_bomb_10s = tempo_decorrido >= 29.8

    if not dm_mode and should_trigger_bomb_10s and _can_play_main_state("bomb_10s"):
        musica_10s = get_music_and_volume(get_config(), "bomb_10s")
        if musica_10s[0]:
            # Enquanto a bomba estiver travada, bomb_10s nao pode trocar a trilha.
            if bomb_music_locked:
                last_bomb_10s = True
            else:
                last_bomb_10s = True
                estado_atual = "bomb_10s"
                tocar_musica(musica_10s[0], musica_10s[1])
                _mark_main_state_played("bomb_10s")
                print(f">>> BOMBA 10S: Volume {musica_10s[1]}%")

    if kills is not None:
        if kills_anteriores is None:
            kills_anteriores = kills
        elif kills > kills_anteriores and kill_detection_armed:
            _play_event(get_config(), "kill", game_mode=game_mode)
            kills_anteriores = kills

    if hp is not None:
            if hp == 0 and hp_anterior > 0:
                _halt_combat_music_on_death()
                if not is_spectating_other and not bomb_music_locked:
                    _play_event(get_config(), "death", game_mode=game_mode)
            hp_anterior = hp
