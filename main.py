import json
import os
import socket
import subprocess
import sys
import time
import tkinter as tk
import ctypes
import atexit
import urllib.error
import urllib.request
from tkinter import filedialog, messagebox
from findmusic import clamp_action_duration, clamp_volume, invalidate_config_cache, invalidate_music_path_cache
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "1"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
EVENTS = [
    "menu",
    "action",
    "freezetime",
    "death",
    "kill",
    "bomb_planted",
    "round_10s",
    "bomb_10s",
    "combat_intense",
    "survival",
    "win_round",
    "loose_round",
]
VISIBLE_EVENTS = [event for event in EVENTS if event != "bomb_10s"]
EVENT_LABELS = {
    "menu": "Menu",
    "action": "Action",
    "freezetime": "Freezetime",
    "death": "Death",
    "kill": "Kill",
    "bomb_planted": "Bomb planted",
    "round_10s": "Round 10s",
    "bomb_10s": "Bomb 10s",
    "combat_intense": "Combat intense",
    "survival": "Survival",
    "win_round": "Win round",
    "loose_round": "Loose round",
}
EFFECT_EVENTS = {"death", "kill", "round_10s"}
RUNTIME_HOST = "127.0.0.1"
RUNTIME_PORT = 3000
runtime_process = None
app_instance = None


def set_process_priority(background=True):
    if os.name != "nt":
        return
    try:
        # 0x00000040 é IDLE_PRIORITY_CLASS. 
        # O programa vai rodar sem tirar 1 FPS do jogo.
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(),
            0x00000040 if background else 0x00000020,
        )
    except Exception:
        pass


def is_runtime_running():
    try:
        with socket.create_connection((RUNTIME_HOST, RUNTIME_PORT), timeout=0.2):
            return True
    except OSError:
        return False


def start_runtime_process():
    global runtime_process

    if is_runtime_running():
        return True

    if getattr(sys, "frozen", False):
        command = [sys.executable, "--runtime"]
    else:
        command = [sys.executable, os.path.abspath(__file__), "--runtime"]

    kwargs = {
        "cwd": BASE_DIR,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000

    try:
        runtime_process = subprocess.Popen(command, **kwargs)
    except Exception as e:
        print("erro ao iniciar runtime:", e)
        return False

    for _ in range(20):
        if is_runtime_running():
            return True
        time.sleep(0.1)

    return is_runtime_running()


def run_headless_runtime():
    set_process_priority(background=True)
    from server import start_server

    start_server()


def stop_runtime_process():
    global runtime_process

    request = urllib.request.Request(
        f"http://{RUNTIME_HOST}:{RUNTIME_PORT}/shutdown",
        data=b"",
        method="POST",
    )

    shutdown_sent = False
    try:
        with urllib.request.urlopen(request, timeout=1):
            shutdown_sent = True
    except (urllib.error.URLError, OSError) as e:
        print("erro ao finalizar runtime:", e)

    if runtime_process is not None:
        try:
            runtime_process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            try:
                runtime_process.terminate()
                runtime_process.wait(timeout=1.5)
            except Exception as terminate_error:
                print("erro ao encerrar processo runtime:", terminate_error)
        except Exception as wait_error:
            print("erro ao aguardar runtime:", wait_error)
        finally:
            runtime_process = None

    return shutdown_sent or not is_runtime_running()


def cleanup_on_exit():
    global app_instance

    if app_instance is not None:
        try:
            app_instance.close_app(force=True)
        except Exception:
            pass
        finally:
            app_instance = None

def create_gsi(exe_path):
    exe_path = exe_path.lower()

    if "csgo.exe" in exe_path:
        print("Detectado: CSGO LEGACY")
        base_dir = os.path.dirname(exe_path)
        cfg_path = os.path.join(
            base_dir,
            "csgo",
            "cfg",
            "gamestate_integration_music.cfg",
        )
    else:
        print("Detectado: CS2")
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(exe_path)))
        cfg_path = os.path.join(
            base_dir,
            "csgo",
            "cfg",
            "gamestate_integration_music.cfg",
        )

    content = """
"Music Integration"
{
 "uri" "http://127.0.0.1:3000"

 "timeout" "0.03"
 "buffer"  "0.0"
 "throttle" "0.1"
 "heartbeat" "0.1"

 "data"
 {
  "provider" "1"
  "map" "1"
  "round" "1"
  "phase_countdowns" "1"
  "player_id" "1"
  "player_state" "1"
  "bomb" "1"
  "player_match_stats" "1"
  "player_weapons" "1"
  "allplayers_id" "1"
 }
}
"""

    try:
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        raise RuntimeError(
            f"nao foi possivel criar o GSI em:\n{cfg_path}\n\n"
            "Tente abrir o programa como administrador ou instalar o jogo em uma pasta com permissao de escrita."
        ) from e

    print("CFG criado em:", cfg_path)
    return cfg_path


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("CS2 Music Kit")
        self.root.geometry("920x760")
        self.root.minsize(860, 620)

        self.files = {}
        self.game_path = ""
        self.music_volume_values = {}
        self.category_volume_values = {}
        self.music_row_value_labels = {}
        self.action_duration_values = {}
        self.action_loop_values = {}
        self.event_widgets = {}
        self.event_expanded = {event: False for event in EVENTS}
        self.rendered_tracks = {event: None for event in EVENTS}
        self.window_hidden = False
        self.scroll_update_pending = False
        self.scroll_update_after_id = None
        self.preview_backend = None
        self.config_save_after_id = None
        self.is_closing = False
        self.loading_config = False

        self.fade_time_var = tk.IntVar(value=1)
        self.action_time_var = tk.IntVar(value=10)
        self.fade_var = tk.BooleanVar(value=True)
        self.menu_next_on_finish_var = tk.BooleanVar(value=False)
        self.action_loop_var = tk.BooleanVar(value=True)

        # Toggle para ativar/desativar combate e sobrevivência
        self.combat_music_enabled_var = tk.BooleanVar(value=True)

        # Variáveis de estresse
        self.stress_max_var = tk.IntVar(value=100)
        self.stress_play_threshold_var = tk.IntVar(value=100)
        self.stress_shoot_per_sec_var = tk.DoubleVar(value=8)
        self.stress_damage_var = tk.IntVar(value=40)
        self.stress_kill_var = tk.IntVar(value=50)
        self.stress_decay_no_shoot_var = tk.IntVar(value=10)
        self.stress_decay_no_damage_var = tk.IntVar(value=10)
        self.hp_survival_low_var = tk.IntVar(value=78)
        self.hp_survival_critical_var = tk.IntVar(value=77)

        self.bind_config_vars()
        self.build_layout()
        self.bind_window_state_events()
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)
        self.load_config()

    def bind_config_vars(self):
        for var in (
            self.fade_time_var,
            self.action_time_var,
            self.fade_var,
            self.menu_next_on_finish_var,
            self.action_loop_var,
            self.combat_music_enabled_var,
            self.stress_max_var,
            self.stress_play_threshold_var,
            self.stress_shoot_per_sec_var,
            self.stress_damage_var,
            self.stress_kill_var,
            self.stress_decay_no_shoot_var,
            self.stress_decay_no_damage_var,
            self.hp_survival_low_var,
            self.hp_survival_critical_var,
        ):
            var.trace_add("write", self.on_config_var_changed)

    def on_config_var_changed(self, *_args):
        if self.loading_config:
            return
        self.schedule_config_save()

    def bind_window_state_events(self):
        self.root.bind("<Unmap>", self.on_window_hidden)
        self.root.bind("<Map>", self.on_window_shown)

    def on_window_hidden(self, _event):
        try:
            hidden = self.root.state() != "normal"
        except tk.TclError:
            hidden = True

        if hidden and not self.window_hidden:
            self.window_hidden = True
            if self.scroll_update_after_id is not None:
                try:
                    self.root.after_cancel(self.scroll_update_after_id)
                except tk.TclError:
                    pass
                self.scroll_update_after_id = None
                self.scroll_update_pending = False
            try:
                self.canvas.itemconfigure(self.canvas_window_id, state="hidden")
            except tk.TclError:
                pass
            set_process_priority(background=True)

    def on_window_shown(self, _event):
        if self.window_hidden:
            self.window_hidden = False
            try:
                self.canvas.itemconfigure(self.canvas_window_id, state="normal")
            except tk.TclError:
                pass
            set_process_priority(background=False)
            self.schedule_scrollregion_update()

    def build_layout(self):
        outer = tk.Frame(self.root, padx=10, pady=10)
        outer.pack(fill="both", expand=True)

        header = tk.LabelFrame(outer, text="Configuration")
        header.pack(fill="x")

        row1 = tk.Frame(header, padx=8, pady=8)
        row1.pack(fill="x")

        tk.Button(row1, text="Select CS2.exe", command=self.load_game, width=18).pack(side="left")
        tk.Button(row1, text="Open CS2", command=self.launch_game, width=12).pack(side="left", padx=(8, 0))
        tk.Button(row1, text="Stop Music", command=self.stop_music, width=12).pack(side="left", padx=(8, 0))
        tk.Button(row1, text="Save Config", command=self.save, width=14).pack(side="right")

        row2 = tk.Frame(header, padx=8, pady=8)
        row2.pack(fill="x", pady=(0, 8))

        self.game_label = tk.Label(row2, text="No game selected", anchor="w")
        self.game_label.pack(fill="x")

        options = tk.Frame(header, padx=8, pady=8)
        options.pack(fill="x", pady=(0, 8))

        tk.Checkbutton(options, text="Fade between tracks", variable=self.fade_var).pack(side="left")
        tk.Checkbutton(
            options,
            text="Switch menu music when it ends",
            variable=self.menu_next_on_finish_var,
        ).pack(side="left", padx=(24, 0))

        tk.Label(options, text="Fade (s):").pack(side="left", padx=(24, 4))
        tk.Entry(options, textvariable=self.fade_time_var, width=5).pack(side="left")

        tk.Label(
            header,
            text="Open the game from here so GSI is configured and events arrive with less delay.",
            fg="red",
            anchor="w",
            padx=8,
            pady=4,
        ).pack(fill="x")

        # Stress Settings section
        stress_frame = tk.LabelFrame(header, text="Stress Settings", padx=8, pady=8)
        stress_frame.pack(fill="x", pady=(8, 0))

        stress_enable_row = tk.Frame(stress_frame)
        stress_enable_row.pack(fill="x", pady=4)
        tk.Checkbutton(
            stress_enable_row,
            text="Enable Combat and Survival Music",
            variable=self.combat_music_enabled_var,
        ).pack(side="left")

        stress_row1 = tk.Frame(stress_frame)
        stress_row1.pack(fill="x", pady=4)
        tk.Label(stress_row1, text="Max Stress:", width=20, anchor="w").pack(side="left")
        tk.Spinbox(stress_row1, from_=1, to=1000, textvariable=self.stress_max_var, width=8).pack(side="left", padx=4)
        
        tk.Label(stress_row1, text="Threshold:", width=15, anchor="w").pack(side="left", padx=(16, 0))
        tk.Spinbox(stress_row1, from_=1, to=1000, textvariable=self.stress_play_threshold_var, width=8).pack(side="left", padx=4)

        stress_row2 = tk.Frame(stress_frame)
        stress_row2.pack(fill="x", pady=4)
        tk.Label(stress_row2, text="Damage:", width=20, anchor="w").pack(side="left")
        tk.Spinbox(stress_row2, from_=1, to=500, textvariable=self.stress_damage_var, width=8).pack(side="left", padx=4)
        
        tk.Label(stress_row2, text="Kill:", width=15, anchor="w").pack(side="left", padx=(16, 0))
        tk.Spinbox(stress_row2, from_=1, to=500, textvariable=self.stress_kill_var, width=8).pack(side="left", padx=4)

        stress_row3 = tk.Frame(stress_frame)
        stress_row3.pack(fill="x", pady=4)
        tk.Label(stress_row3, text="Shoot per sec:", width=20, anchor="w").pack(side="left")
        tk.Spinbox(stress_row3, from_=0.1, to=100, textvariable=self.stress_shoot_per_sec_var, width=8).pack(side="left", padx=4)
        
        tk.Label(stress_row3, text="Decay Shoot:", width=15, anchor="w").pack(side="left", padx=(16, 0))
        tk.Spinbox(stress_row3, from_=0.1, to=100, textvariable=self.stress_decay_no_shoot_var, width=8).pack(side="left", padx=4)

        stress_row4 = tk.Frame(stress_frame)
        stress_row4.pack(fill="x", pady=4)
        tk.Label(stress_row4, text="Decay Damage:", width=20, anchor="w").pack(side="left")
        tk.Spinbox(stress_row4, from_=0.1, to=100, textvariable=self.stress_decay_no_damage_var, width=8).pack(side="left", padx=4)

        stress_row5 = tk.Frame(stress_frame)
        stress_row5.pack(fill="x", pady=4)
        tk.Label(stress_row5, text="HP Low:", width=20, anchor="w").pack(side="left")
        tk.Spinbox(stress_row5, from_=1, to=200, textvariable=self.hp_survival_low_var, width=8).pack(side="left", padx=4)
        
        tk.Label(stress_row5, text="HP Critical:", width=15, anchor="w").pack(side="left", padx=(16, 0))
        tk.Spinbox(stress_row5, from_=1, to=200, textvariable=self.hp_survival_critical_var, width=8).pack(side="left", padx=4)

        main_frame = tk.Frame(outer)
        main_frame.pack(fill="both", expand=True, pady=(10, 0))

        self.canvas = tk.Canvas(main_frame, highlightthickness=0)
        scrollbar = tk.Scrollbar(main_frame, orient="vertical", command=self.canvas.yview)
        self.scroll_frame = tk.Frame(self.canvas)

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: self.schedule_scrollregion_update(),
        )

        self.canvas_window_id = self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-(e.delta / 120)), "units"))

        for event in VISIBLE_EVENTS:
            self.create_event_section(event)

    def schedule_scrollregion_update(self):
        if self.window_hidden or self.scroll_update_pending:
            return
        self.scroll_update_pending = True
        self.scroll_update_after_id = self.root.after_idle(self.update_scrollregion)

    def update_scrollregion(self):
        self.scroll_update_after_id = None
        self.scroll_update_pending = False
        if self.window_hidden:
            return
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def preload_effects_in_background(self):
        return

    def get_preview_backend(self):
        if self.preview_backend is None:
            import playmusic

            self.preview_backend = playmusic
        return self.preview_backend

    def create_event_section(self, event):
        frame = tk.LabelFrame(self.scroll_frame, text=EVENT_LABELS[event], padx=8, pady=8)
        frame.pack(fill="x", pady=6)

        top = tk.Frame(frame)
        top.pack(fill="x")

        count_label = tk.Label(top, text="0 tracks", width=12, anchor="w")
        count_label.pack(side="left")

        category_frame = tk.Frame(top)
        category_frame.pack(side="right")

        tk.Label(category_frame, text="Category vol:").pack(side="left")
        category_value_label = tk.Label(category_frame, text=f"{self.get_category_volume(event)}%", width=5, anchor="e")
        category_value_label.pack(side="left", padx=(4, 0))
        tk.Scale(
            category_frame,
            from_=0,
            to=100,
            orient="horizontal",
            length=140,
            variable=self.get_category_volume_var(event),
            command=lambda value, ev=event, lbl=category_value_label: self.update_category_volume(ev, value, lbl),
        ).pack(side="left", padx=(6, 0))

        tk.Button(top, text="Add", command=lambda ev=event: self.load_music(ev), width=12).pack(side="right")
        toggle_button = tk.Button(
            top,
            text="Collapse",
            command=lambda ev=event: self.toggle_event_section(ev),
            width=12,
        )
        toggle_button.pack(side="right", padx=(0, 8))

        content_frame = tk.Frame(frame)

        hint_label = tk.Label(
            content_frame,
            text="Adjust the volume of each track directly in the list below.",
            anchor="w",
        )
        hint_label.pack(fill="x", pady=(0, 6))

        tracks_frame = tk.Frame(content_frame)
        tracks_frame.pack(fill="x")

        self.event_widgets[event] = {
            "count": count_label,
            "content_frame": content_frame,
            "hint": hint_label,
            "tracks_frame": tracks_frame,
            "toggle_button": toggle_button,
            "category_volume_label": category_value_label,
        }

    def load_game(self):
        file = filedialog.askopenfilename(filetypes=[("Executable", "*.exe")])

        if file:
            self.game_path = file
            self.game_label.config(text=os.path.basename(file))
            self.ensure_gsi(show_success=True)

    def ensure_gsi(self, show_success=False):
        if not self.game_path:
            return False

        try:
            cfg_path = create_gsi(self.game_path)
        except RuntimeError as e:
            messagebox.showerror("Erro ao configurar GSI", str(e))
            print(e)
            return False

        if show_success:
            messagebox.showinfo("GSI configurado", f"Arquivo criado em:\n{cfg_path}")
        return True

    def get_music_volume_var(self, event, music, value=None):
        event_vars = self.music_volume_values.setdefault(event, {})
        if music not in event_vars:
            default_value = 100 if value is None else clamp_volume(value)
            event_vars[music] = tk.IntVar(value=default_value)
        return event_vars[music]

    def get_music_volume(self, event, music):
        return clamp_volume(self.get_music_volume_var(event, music).get())

    def get_category_volume_var(self, event):
        if event not in self.category_volume_values:
            var = tk.IntVar(value=100)
            var.trace_add("write", lambda *_args, ev=event: self.on_category_volume_changed(ev))
            self.category_volume_values[event] = var
        return self.category_volume_values[event]

    def get_category_volume(self, event):
        return clamp_volume(self.get_category_volume_var(event).get())

    def on_category_volume_changed(self, event):
        if self.loading_config:
            return

        try:
            volume = clamp_volume(self.get_category_volume_var(event).get())
        except (TypeError, ValueError):
            return

        if event in self.event_widgets:
            label = self.event_widgets[event].get("category_volume_label")
            if label is not None:
                try:
                    label.config(text=f"{volume}%")
                except tk.TclError:
                    pass

        for music in self.files.get(event, []):
            volume_var = self.get_music_volume_var(event, music)
            if volume_var.get() != volume:
                volume_var.set(volume)
            label = self.music_row_value_labels.get((event, music))
            if label is not None:
                try:
                    label.config(text=f"{volume}%")
                except tk.TclError:
                    pass

        backend = self.preview_backend
        if backend:
            for music in self.files.get(event, []):
                if event in EFFECT_EVENTS:
                    backend.set_effect_volume(music, volume)
                elif backend.get_current_music() == music:
                    backend.set_volume(volume)

        self.schedule_config_save()

    def get_action_duration_var(self, music):
        if music not in self.action_duration_values:
            default_duration = clamp_action_duration(self.action_time_var.get())
            var = tk.IntVar(value=default_duration)
            var.trace_add("write", lambda *_args, m=music: self.on_action_duration_changed(m))
            self.action_duration_values[music] = var
        return self.action_duration_values[music]

    def get_action_duration(self, music):
        try:
            value = self.get_action_duration_var(music).get()
        except tk.TclError:
            return clamp_action_duration(self.action_time_var.get())
        return clamp_action_duration(value)

    def get_action_loop_var(self, music):
        if music not in self.action_loop_values:
            var = tk.BooleanVar(value=self.action_loop_var.get())
            var.trace_add("write", lambda *_args: self.on_config_var_changed())
            self.action_loop_values[music] = var
        return self.action_loop_values[music]

    def get_action_loop(self, music):
        try:
            return bool(self.get_action_loop_var(music).get())
        except tk.TclError:
            return bool(self.action_loop_var.get())

    def on_action_duration_changed(self, music):
        if self.loading_config:
            return
        duration_var = self.get_action_duration_var(music)
        try:
            current_value = duration_var.get()
        except tk.TclError:
            return
        duration = clamp_action_duration(current_value)
        if current_value != duration:
            duration_var.set(duration)
            return
        self.schedule_config_save()

    def update_music_volume_realtime(self, event, music, value):
        try:
            volume = clamp_volume(float(value))
        except (TypeError, ValueError):
            return

        volume_var = self.get_music_volume_var(event, music)
        if volume_var.get() != volume:
            volume_var.set(volume)

        backend = self.preview_backend
        if backend and event in EFFECT_EVENTS:
            backend.set_effect_volume(music, volume)
            return

        if backend and backend.get_current_music() == music:
            backend.set_volume(volume)

    def refresh_event_ui(self, event):
        widget = self.event_widgets[event]
        musicas = self.files.get(event, [])
        tracks_frame = widget["tracks_frame"]

        total = len(musicas)
        widget["count"].config(text="1 track" if total == 1 else f"{total} tracks")

        if not self.event_expanded.get(event, False):
            widget["hint"].config(text="Click Expand to see the tracks and volumes.")
            self.update_event_visibility(event)
            return

        if not musicas:
            self.clear_tracks_if_needed(event)
            widget["hint"].config(text="No tracks added yet.")
            self.update_event_visibility(event)
            return

        widget["hint"].config(text="Adjust the volume of each track directly in the list below.")
        current_signature = tuple(musicas)
        if self.rendered_tracks.get(event) != current_signature:
            self.clear_tracks_if_needed(event)
            for musica in musicas:
                self.create_music_row(event, tracks_frame, musica)
            self.rendered_tracks[event] = current_signature

        self.update_event_visibility(event)

    def clear_tracks_if_needed(self, event):
        tracks_frame = self.event_widgets[event]["tracks_frame"]
        if tracks_frame.winfo_children():
            for child in tracks_frame.winfo_children():
                child.destroy()
        for key in list(self.music_row_value_labels):
            if key[0] == event:
                self.music_row_value_labels.pop(key, None)
        self.rendered_tracks[event] = ()

    def toggle_event_section(self, event):
        self.event_expanded[event] = not self.event_expanded[event]
        self.refresh_event_ui(event)

    def update_event_visibility(self, event):
        widget = self.event_widgets[event]
        if self.event_expanded.get(event, True):
            if not widget["content_frame"].winfo_manager():
                widget["content_frame"].pack(fill="x", pady=(8, 0))
            widget["toggle_button"].config(text="Collapse")
        else:
            if widget["content_frame"].winfo_manager():
                widget["content_frame"].pack_forget()
            widget["toggle_button"].config(text="Expand")
        self.schedule_scrollregion_update()

    def create_music_row(self, event, parent, musica):
        row = tk.Frame(parent, bd=1, relief="groove", padx=8, pady=6)
        row.pack(fill="x", pady=3)

        top_row = tk.Frame(row)
        top_row.pack(fill="x")

        name_label = tk.Label(top_row, text=os.path.basename(musica), anchor="w")
        name_label.pack(side="left", fill="x", expand=True)

        tk.Button(
            top_row,
            text="Remove",
            width=10,
            command=lambda ev=event, m=musica: self.remove_music(ev, m),
        ).pack(side="right")
        tk.Button(
            top_row,
            text="Test",
            width=10,
            command=lambda ev=event, m=musica: self.play_selected_music(ev, m),
        ).pack(side="right", padx=(0, 8))

        slider_row = tk.Frame(row)
        slider_row.pack(fill="x", pady=(6, 0))

        tk.Label(slider_row, text="Volume:").pack(side="left")

        volume_var = self.get_music_volume_var(event, musica)
        value_label = tk.Label(slider_row, text=f"{volume_var.get()}%", width=5, anchor="e")
        value_label.pack(side="right")
        self.music_row_value_labels[(event, musica)] = value_label

        volume_scale = tk.Scale(
            slider_row,
            from_=0,
            to=100,
            orient="horizontal",
            variable=volume_var,
            length=280,
            command=lambda value, ev=event, m=musica, lbl=value_label: self.update_music_row_volume(ev, m, value, lbl),
        )
        volume_scale.pack(side="right", fill="x", expand=True, padx=(8, 8))

        if event == "action":
            duration_row = tk.Frame(row)
            duration_row.pack(fill="x", pady=(6, 0))

            tk.Checkbutton(
                duration_row,
                text="Loop action",
                variable=self.get_action_loop_var(musica),
            ).pack(side="left")
            tk.Label(duration_row, text="Round duration (s):").pack(side="left")
            tk.Spinbox(
                duration_row,
                from_=1,
                to=600,
                width=6,
                textvariable=self.get_action_duration_var(musica),
                command=self.schedule_config_save,
            ).pack(side="left", padx=(8, 0))

    def update_music_row_volume(self, event, music, value, label):
        self.update_music_volume_realtime(event, music, value)
        label.config(text=f"{self.get_music_volume(event, music)}%")
        self.schedule_config_save()

    def update_category_volume(self, event, value, label):
        try:
            volume = clamp_volume(float(value))
        except (TypeError, ValueError):
            return

        self.get_category_volume_var(event).set(volume)
        label.config(text=f"{volume}%")

    def load_config(self):
        self.loading_config = True
        if not os.path.exists(CONFIG_FILE):
            for event in EVENTS:
                self.files.setdefault(event, [])
                if event in self.event_widgets:
                    self.refresh_event_ui(event)
            self.loading_config = False
            return

        with open(CONFIG_FILE, encoding="utf-8") as f:
            config = json.load(f)

        self.game_path = config.get("game_path", "")
        loaded_files = config.get("files", {})
        old_end_round_files = loaded_files.get("end_round", [])
        self.files = {
            event: loaded_files.get(event, [])
            for event in EVENTS
        }
        if old_end_round_files:
            if not self.files.get("win_round"):
                self.files["win_round"] = list(old_end_round_files)
            if not self.files.get("loose_round"):
                self.files["loose_round"] = list(old_end_round_files)
        self.fade_time_var.set(config.get("fade_time", 1))
        self.fade_var.set(config.get("fade", True))
        self.menu_next_on_finish_var.set(config.get("menu_next_on_finish", False))
        self.action_loop_var.set(config.get("action_loop", True))
        self.action_time_var.set(config.get("action_time", 10))

        # Carregar configurações de combate
        self.combat_music_enabled_var.set(config.get("combat_music_enabled", True))

        # Carregar configurações de estresse
        stress_settings = config.get("stress_settings", {})
        self.stress_max_var.set(stress_settings.get("stress_max", 100))
        self.stress_play_threshold_var.set(stress_settings.get("stress_play_threshold", 100))
        self.stress_shoot_per_sec_var.set(stress_settings.get("stress_shoot_per_sec", 8))
        self.stress_damage_var.set(stress_settings.get("stress_damage", 40))
        self.stress_kill_var.set(stress_settings.get("stress_kill", 50))
        self.stress_decay_no_shoot_var.set(stress_settings.get("stress_decay_no_shoot", 10))
        self.stress_decay_no_damage_var.set(stress_settings.get("stress_decay_no_damage", 10))
        self.hp_survival_low_var.set(stress_settings.get("hp_survival_low", 78))
        self.hp_survival_critical_var.set(stress_settings.get("hp_survival_critical", 77))

        invalidate_music_path_cache()

        if self.game_path:
            self.game_label.config(text=os.path.basename(self.game_path))

        raw_music_volumes = config.get("music_volumes", {})
        music_volumes = {
            event: raw_music_volumes.get(event, {})
            for event in EVENTS
        }
        category_volumes = config.get("category_volumes", {})
        raw_action_durations = config.get("action_durations", {})
        raw_action_loops = config.get("action_loops", {})

        for event in EVENTS:
            if event not in self.files or not isinstance(self.files[event], list):
                self.files[event] = []

            stored_category_volume = category_volumes.get(event, 100)
            self.get_category_volume_var(event).set(clamp_volume(stored_category_volume))

            for music in self.files[event]:
                raw_value = music_volumes.get(event, {}).get(music)
                if raw_value is None:
                    default_volume = self.get_category_volume(event)
                    self.get_music_volume_var(event, music, value=default_volume).set(default_volume)
                else:
                    self.get_music_volume_var(event, music).set(clamp_volume(raw_value))

            if event == "action":
                for music in self.files[event]:
                    duration = raw_action_durations.get(music, self.action_time_var.get())
                    self.get_action_duration_var(music).set(clamp_action_duration(duration))
                    loop = raw_action_loops.get(music, self.action_loop_var.get())
                    self.get_action_loop_var(music).set(bool(loop))

            if event in self.event_widgets:
                self.refresh_event_ui(event)

        self.preload_effects_in_background()
        self.loading_config = False
        print("config carregado")

    def load_music(self, event):
        selected_files = filedialog.askopenfilenames(filetypes=[("Music", "*.mp3 *.wav *.ogg")])
        if not selected_files:
            return

        current_files = list(self.files.get(event, []))
        known_files = set(current_files)

        for file in selected_files:
            if file not in known_files:
                current_files.append(file)
                known_files.add(file)
                default_volume = self.get_category_volume(event)
                self.get_music_volume_var(event, file, value=default_volume).set(default_volume)
                if event == "action":
                    self.get_action_duration_var(file).set(clamp_action_duration(self.action_time_var.get()))
                    self.get_action_loop_var(file).set(self.action_loop_var.get())

        self.files[event] = current_files
        self.refresh_event_ui(event)

    def play_selected_music(self, event, caminho):
        if not caminho or not os.path.isfile(caminho):
            print("nenhum audio encontrado")
            return

        volume = self.get_music_volume(event, caminho)
        print(f"Testando {event} -> {os.path.basename(caminho)}")
        if event in EFFECT_EVENTS:
            self._play_effect_worker(caminho, volume)
        else:
            loop = event not in ("win_round", "loose_round")
            duration = None
            if event == "menu":
                loop = not self.menu_next_on_finish_var.get()
            elif event == "action":
                loop = self.get_action_loop(caminho)
                duration = None if loop else self.get_action_duration(caminho)
            self._play_music_worker(caminho, volume, loop=loop, duration=duration)

    def _play_music_worker(self, caminho, volume, loop=True, duration=None):
        backend = self.get_preview_backend()
        backend.parar_efeitos()
        backend.tocar_musica(caminho, volume, loop=loop, duration=duration)

    def _play_effect_worker(self, caminho, volume):
        backend = self.get_preview_backend()
        backend.tocar_efeito(caminho, volume)

    def remove_music(self, event, musica):
        musicas = list(self.files.get(event, []))
        if musica in musicas:
            musicas.remove(musica)
            self.files[event] = musicas

        if event in self.music_volume_values:
            self.music_volume_values[event].pop(musica, None)
        self.music_row_value_labels.pop((event, musica), None)
        if event == "action":
            self.action_duration_values.pop(musica, None)
            self.action_loop_values.pop(musica, None)

        self.rendered_tracks[event] = None
        self.refresh_event_ui(event)

    def build_config_data(self):
        return {
            "game_path": self.game_path,
            "files": self.files,
            "music_volumes": {
                event: {music: var.get() for music, var in music_vars.items()}
                for event, music_vars in self.music_volume_values.items()
                if music_vars
            },
            "category_volumes": {
                event: self.get_category_volume(event)
                for event in EVENTS
            },
            "action_durations": {
                music: self.get_action_duration(music)
                for music in self.files.get("action", [])
            },
            "action_loops": {
                music: self.get_action_loop(music)
                for music in self.files.get("action", [])
            },
            "action_loop": self.action_loop_var.get(),
            "menu_next_on_finish": self.menu_next_on_finish_var.get(),
            "fade": self.fade_var.get(),
            "action_time": self.action_time_var.get(),
            "fade_time": self.fade_time_var.get(),
            "combat_music_enabled": self.combat_music_enabled_var.get(),
            "stress_settings": {
                "stress_max": self.stress_max_var.get(),
                "stress_play_threshold": self.stress_play_threshold_var.get(),
                "stress_shoot_per_sec": self.stress_shoot_per_sec_var.get(),
                "stress_damage": self.stress_damage_var.get(),
                "stress_kill": self.stress_kill_var.get(),
                "stress_decay_no_shoot": self.stress_decay_no_shoot_var.get(),
                "stress_decay_no_damage": self.stress_decay_no_damage_var.get(),
                "hp_survival_low": self.hp_survival_low_var.get(),
                "hp_survival_critical": self.hp_survival_critical_var.get(),
            },
        }

    def write_config(self):
        config = self.build_config_data()

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)

        invalidate_config_cache()

    def schedule_config_save(self):
        if self.config_save_after_id is not None:
            self.root.after_cancel(self.config_save_after_id)
        self.config_save_after_id = self.root.after(250, self.save_config_silently)

    def save_config_silently(self):
        self.config_save_after_id = None
        self.write_config()

    def save(self):
        self.write_config()
        if self.game_path and not self.ensure_gsi():
            return

        print("config salva")
        messagebox.showinfo("Config", "Configuracao salva com sucesso.")

    def launch_game(self):
        if not self.game_path:
            print("nenhum jogo selecionado")
            messagebox.showwarning("Jogo nao selecionado", "Selecione o CS2.exe antes de abrir o jogo.")
            return

        try:
            if self.config_save_after_id is not None:
                self.root.after_cancel(self.config_save_after_id)
                self.config_save_after_id = None
            self.write_config()

            if not self.ensure_gsi():
                return

            if not start_runtime_process():
                messagebox.showerror("Erro ao iniciar runtime", "Nao foi possivel iniciar o runtime de audio em segundo plano.")
                return

            path = self.game_path.lower()
            if "csgo.exe" in path:
                print("abrindo CSGO legacy...")
                os.startfile("steam://rungameid/4465480")
            else:
                print("abrindo CS2...")
                os.startfile("steam://rungameid/730")

            self.root.iconify()
        except Exception as e:
            print("erro ao abrir:", e)

    def stop_music(self):
        if not self.preview_backend:
            return
        self.preview_backend.parar_efeitos()
        self.preview_backend.parar_musica()

    def close_app(self, force=False):
        if self.is_closing:
            return
        self.is_closing = True

        try:
            if self.config_save_after_id is not None:
                self.root.after_cancel(self.config_save_after_id)
                self.config_save_after_id = None
                self.save_config_silently()
        except tk.TclError:
            pass

        try:
            self.stop_music()
        except Exception:
            pass

        try:
            if is_runtime_running():
                stop_runtime_process()
        except Exception as e:
            print("erro ao finalizar runtime no fechamento:", e)

        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass

        if not force:
            raise SystemExit


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    if "--runtime" in sys.argv:
        run_headless_runtime()
    else:
        atexit.register(cleanup_on_exit)
        set_process_priority(background=False)
        root = tk.Tk()
        app = App(root)
        app_instance = app
        root.mainloop()
