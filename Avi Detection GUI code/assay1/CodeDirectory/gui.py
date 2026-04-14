#!/usr/bin/env python3
"""
Refactored GUI for Drosophila Genetics Project

Clean architecture with controller separation and improved layout.
"""

import os
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import time
import logging

# Import system components from existing files
from motion import get_hardware_controller, GPIO_AVAILABLE
from nozzle_implementation import SystemController, SystemState
import config

logger = logging.getLogger(__name__)

class SliderSwitch(tk.Canvas):
    """Custom slider switch widget"""
    def __init__(self, parent, command=None, initial=False, width=72, height=32, on_text="ON", off_text="OFF", **kwargs):
        super().__init__(parent, width=width, height=height, highlightthickness=0, **kwargs)
        self.command = command
        self.value = bool(initial)
        self.width = width
        self.height = height
        self.on_text = on_text
        self.off_text = off_text
        self.bind("<Button-1>", self.toggle)
        self.draw()

    def toggle(self, event=None):
        self.value = not self.value
        self.draw()
        if callable(self.command):
            self.command(self.value)

    def draw(self):
        self.delete("all")
        track_color = "#4CAF50" if self.value else "#f44336"
        radius = self.height / 2
        self.create_oval(0, 0, self.height, self.height, fill=track_color, outline=track_color)
        self.create_oval(self.width - self.height, 0, self.width, self.height, fill=track_color, outline=track_color)
        self.create_rectangle(radius, 0, self.width - radius, self.height, fill=track_color, outline=track_color)
        knob_x = self.width - self.height + 2 if self.value else 2
        self.create_oval(knob_x, 2, knob_x + self.height - 4, self.height - 2, fill="white", outline="#cccccc")
        label = self.on_text if self.value else self.off_text
        self.create_text(self.width / 2, self.height / 2, text=label, fill="white", font=("Arial", 8, "bold"))

class DrosophilaGUI:
    def __init__(self, root):
        self.root = root
        mode = " (Simulation Mode)" if not GPIO_AVAILABLE else ""
        self.root.title(f"Drosophila Genetics Control Panel{mode}")
        self.root.geometry("900x750")  # Increased size for better layout

        # Initialize system controller
        self.controller = SystemController(simulate=not GPIO_AVAILABLE)
        self.controller.add_status_callback(self.update_status_display)

        # GUI state
        self.preview_image = None
        self.last_preview_mtime = None

        self.create_widgets()
        self.start_background_tasks()

    def create_widgets(self):
        """Create the main GUI layout"""
        style = ttk.Style()
        self.configure_styles(style)

        # Main container with padding
        main_frame = ttk.Frame(self.root, padding="15")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Configure grid weights
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        main_frame.columnconfigure(2, weight=1)
        main_frame.rowconfigure(0, weight=0)  # Status
        main_frame.rowconfigure(1, weight=1)  # Main content
        main_frame.rowconfigure(2, weight=0)  # System controls
        main_frame.rowconfigure(3, weight=1)  # Log

        # Status bar (top)
        self.create_status_section(main_frame)

        # Main content area (middle)
        self.create_main_content(main_frame)

        # System controls (bottom above log)
        self.create_system_controls(main_frame)

        # Activity log (bottom)
        self.create_log_section(main_frame)

    def configure_styles(self, style):
        """Configure ttk styles"""
        style.configure("Status.TLabelframe", background="#e8f4f8", relief="raised", borderwidth=2)
        style.configure("Motion.TLabelframe", background="#f0f8e8", relief="raised", borderwidth=2)
        style.configure("Device.TLabelframe", background="#fff8e8", relief="raised", borderwidth=2)
        style.configure("Ops.TLabelframe", background="#f8e8f0", relief="raised", borderwidth=2)
        style.configure("Log.TLabelframe", background="#f8f8f8", relief="sunken", borderwidth=1)
        style.configure("Title.TLabel", font=("Arial", 16, "bold"), foreground="#2E3B4E")
        style.configure("Status.TLabel", font=("Arial", 10, "bold"))
        style.configure("Value.TLabel", font=("Arial", 10), background="#ffffff", relief="sunken", padding=3)

    def create_status_section(self, parent):
        """Create the status section at the top"""
        status_frame = ttk.LabelFrame(parent, text="System Status", style="Status.TLabelframe", padding="10")
        status_frame.grid(row=0, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))

        # Status indicators
        ttk.Label(status_frame, text="State:", style="Status.TLabel").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.state_label = tk.Label(status_frame, text="IDLE", bg="#4CAF50", fg="white", relief="sunken",
                                   font=("Arial", 10, "bold"), padx=10, pady=2)
        self.state_label.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Position:", style="Status.TLabel").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.pos_label = tk.Label(status_frame, text="0.00 mm", bg="#ffffff", relief="sunken",
                                 font=("Arial", 10), padx=5, pady=2)
        self.pos_label.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        ttk.Label(status_frame, text="Message:", style="Status.TLabel").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.message_label = tk.Label(status_frame, text="Ready", bg="#4CAF50", fg="white", relief="sunken",
                                     font=("Arial", 10), padx=5, pady=2, anchor="w")
        self.message_label.grid(row=2, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        mode_text = "Hardware Mode" if GPIO_AVAILABLE else "Simulation Mode"
        mode_color = "#4CAF50" if GPIO_AVAILABLE else "#FF9800"
        ttk.Label(status_frame, text="Mode:", style="Status.TLabel").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.mode_label = tk.Label(status_frame, text=mode_text, bg=mode_color, fg="white", relief="sunken",
                                  font=("Arial", 10), padx=5, pady=2)
        self.mode_label.grid(row=3, column=1, sticky=(tk.W, tk.E), padx=(10, 0), pady=2)

        status_frame.columnconfigure(1, weight=1)

    def create_main_content(self, parent):
        """Create the main content area with motion, preview, and controls"""
        content_frame = ttk.Frame(parent)
        content_frame.grid(row=1, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 10))
        content_frame.columnconfigure(0, weight=0)  # Motion
        content_frame.columnconfigure(1, weight=1)  # Preview
        content_frame.columnconfigure(2, weight=0)  # Controls
        content_frame.rowconfigure(0, weight=1)

        # Motion control (left)
        self.create_motion_control(content_frame)

        # Channel preview (center)
        self.create_channel_preview(content_frame)

        # Device + Operations controls (right)
        self.create_device_operations(content_frame)

    def create_motion_control(self, parent):
        """Create motion control panel"""
        motion_frame = ttk.LabelFrame(parent, text="Motion Control", style="Motion.TLabelframe", padding="10")
        motion_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=(0, 8))

        # Home button
        tk.Button(motion_frame, text="🏠 Home", bg="#4CAF50", fg="white", font=("Arial", 10, "bold"),
                 relief="raised", command=self.home_gantry).grid(row=0, column=0, pady=3, sticky=(tk.W, tk.E))

        # Position buttons
        positions = [
            ("📍 Channel", config.CHANNEL_CENTER),
            ("🔬 Chamber", config.CHAMBER_CENTER),
            ("🧪 Tube 1", config.TUBE_1_CENTER),
            ("🧪 Tube 2", config.TUBE_2_CENTER),
            ("🧪 Tube 3", config.TUBE_3_CENTER),
            ("🧪 Tube 4", config.TUBE_4_CENTER),
            ("🧪 Tube 5", config.TUBE_5_CENTER),
        ]

        for i, (label, pos) in enumerate(positions, 1):
            tk.Button(motion_frame, text=label, bg="#2196F3", fg="white", font=("Arial", 10, "bold"),
                     relief="raised", command=lambda p=pos: self.move_to_position(p)).grid(
                row=i, column=0, pady=2, sticky=(tk.W, tk.E))

        # Manual move
        ttk.Separator(motion_frame, orient="horizontal").grid(row=8, column=0, sticky=(tk.W, tk.E), pady=8)
        ttk.Label(motion_frame, text="Manual Move (mm):", font=("Arial", 9, "bold")).grid(row=9, column=0, pady=(5, 2), sticky=tk.W)
        self.manual_move_entry = ttk.Entry(motion_frame, width=12, font=("Arial", 10))
        self.manual_move_entry.grid(row=10, column=0, pady=2, sticky=(tk.W, tk.E))
        tk.Button(motion_frame, text="Move Relative", bg="#607D8B", fg="white", font=("Arial", 10, "bold"),
                 relief="raised", command=self.manual_move).grid(row=11, column=0, pady=3, sticky=(tk.W, tk.E))

    def create_channel_preview(self, parent):
        """Create channel detection preview panel"""
        preview_frame = ttk.LabelFrame(parent, text="Channel Detection Preview", style="Log.TLabelframe", padding="10")
        preview_frame.grid(row=0, column=1, sticky=(tk.W, tk.E, tk.N, tk.S), padx=8)
        self.preview_label = tk.Label(preview_frame, text="Waiting for channel detection image...",
                                    bg="#000000", fg="white", font=("Arial", 10, "bold"),
                                    width=48, height=20, anchor=tk.CENTER)
        self.preview_label.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

    def create_device_operations(self, parent):
        """Create device and operations controls (right side)"""
        controls_frame = ttk.Frame(parent)
        controls_frame.grid(row=0, column=2, sticky=(tk.N, tk.S))
        controls_frame.columnconfigure(0, weight=1)

        # Device control
        device_frame = ttk.LabelFrame(controls_frame, text="Device Control", style="Device.TLabelframe", padding="10")
        device_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 0))

        # Vacuum
        ttk.Label(device_frame, text="Vacuum:", font=("Arial", 9, "bold")).grid(row=0, column=0, pady=(0, 3), sticky=tk.W)
        self.vacuum_switch = SliderSwitch(device_frame, command=self.controller.hardware.set_vacuum, initial=False)
        self.vacuum_switch.grid(row=1, column=0, pady=2, sticky=tk.W)

        # Vibration
        ttk.Label(device_frame, text="Vibration:", font=("Arial", 9, "bold")).grid(row=2, column=0, pady=(12, 3), sticky=tk.W)
        self.vibration_switch = SliderSwitch(device_frame, command=self.controller.hardware.set_vibration, initial=False)
        self.vibration_switch.grid(row=3, column=0, pady=2, sticky=tk.W)

        # Operations
        ops_frame = ttk.LabelFrame(controls_frame, text="Operations", style="Ops.TLabelframe", padding="10")
        ops_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=(0, 0))

        tk.Button(ops_frame, text="🚀 Run Automated", bg="#9C27B0", fg="white", font=("Arial", 11, "bold"),
                 relief="raised", command=self.run_automated).grid(row=0, column=0, pady=3, sticky=(tk.W, tk.E))
        tk.Button(ops_frame, text="🧪 Run Assay", bg="#9C27B0", fg="white", font=("Arial", 11, "bold"),
                 relief="raised", command=self.run_assay).grid(row=1, column=0, pady=3, sticky=(tk.W, tk.E))
        tk.Button(ops_frame, text="📷 Classify Fly", bg="#9C27B0", fg="white", font=("Arial", 11, "bold"),
                 relief="raised", command=self.classify_fly_gui).grid(row=2, column=0, pady=3, sticky=(tk.W, tk.E))

    def create_system_controls(self, parent):
        """Create system control buttons"""
        system_frame = ttk.LabelFrame(parent, text="System Control", padding="10")
        system_frame.grid(row=2, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))

        tk.Button(system_frame, text="▶️ START", bg="#4CAF50", fg="white", font=("Arial", 12, "bold"),
                 relief="raised", command=self.system_start).grid(row=0, column=0, pady=3, padx=5, sticky=(tk.W, tk.E))
        tk.Button(system_frame, text="⏹️ STOP", bg="#f44336", fg="white", font=("Arial", 12, "bold"),
                 relief="raised", command=self.system_stop).grid(row=0, column=1, pady=3, padx=5, sticky=(tk.W, tk.E))
        tk.Button(system_frame, text="🔄 RESET", bg="#FF9800", fg="white", font=("Arial", 12, "bold"),
                 relief="raised", command=self.system_reset).grid(row=0, column=2, pady=3, padx=5, sticky=(tk.W, tk.E))

        system_frame.columnconfigure(0, weight=1)
        system_frame.columnconfigure(1, weight=1)
        system_frame.columnconfigure(2, weight=1)

    def create_log_section(self, parent):
        """Create activity log section"""
        log_frame = ttk.LabelFrame(parent, text="Activity Log", style="Log.TLabelframe", padding="10")
        log_frame.grid(row=3, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.S), pady=(15, 0))

        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, wrap=tk.WORD,
                                                 font=("Consolas", 9), bg="#f8f8f8", relief="sunken", borderwidth=1)
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.S))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

    def start_background_tasks(self):
        """Start background update tasks"""
        self.update_position()
        self.update_channel_preview()

    def update_status_display(self, state: str, message: str):
        """Update status display from controller"""
        self.state_label.config(text=state.upper())
        self.message_label.config(text=message)
        self.update_status_color(state)
        self.log_message(f"[{state}] {message}")

    def update_status_color(self, state: str):
        """Update status label colors based on state"""
        colors = {
            "idle": "#4CAF50",
            "running": "#2196F3",
            "detecting": "#FF9800",
            "moving": "#FF9800",
            "picking": "#FF9800",
            "assaying": "#9C27B0",
            "error": "#f44336",
            "stopped": "#607D8B"
        }
        color = colors.get(state.lower(), "#9E9E9E")
        self.state_label.config(bg=color)
        self.message_label.config(bg=color)

    def update_position(self):
        """Update position display"""
        try:
            pos = self.controller.hardware.get_gantry_position()
            self.pos_label.config(text=f"{pos:.2f} mm")
        except Exception as e:
            logger.error(f"Position update error: {e}")
        self.root.after(1000, self.update_position)

    def update_channel_preview(self):
        """Update channel detection preview"""
        try:
            preview_path = os.path.join(os.path.dirname(__file__), config.CHANNEL_DETECTION_PREVIEW_PATH)
            if os.path.exists(preview_path):
                mtime = os.path.getmtime(preview_path)
                if mtime != self.last_preview_mtime:
                    self.load_channel_preview(preview_path)
                    self.last_preview_mtime = mtime
            else:
                self.set_preview_placeholder("Waiting for channel detection image...")
        except Exception as e:
            logger.error(f"Preview update error: {e}")
            self.set_preview_placeholder("Preview unavailable")
        self.root.after(1000, self.update_channel_preview)

    def load_channel_preview(self, path):
        """Load and display preview image"""
        try:
            from PIL import Image, ImageTk
            image = Image.open(path)
            image = image.convert("RGB")
            image.thumbnail((420, 300), Image.ANTIALIAS)
            self.preview_image = ImageTk.PhotoImage(image)
            self.preview_label.config(image=self.preview_image, text="")
        except Exception as e:
            logger.error(f"Failed to load preview: {e}")
            self.set_preview_placeholder("Invalid preview image")

    def set_preview_placeholder(self, message):
        """Set placeholder text for preview"""
        self.preview_label.config(image="", text=message, bg="#000000", fg="white")

    def log_message(self, message):
        """Add message to log"""
        timestamp = time.strftime('%H:%M:%S')
        self.log_text.insert(tk.END, f"{timestamp} - {message}\n")
        self.log_text.see(tk.END)

    # Command handlers
    def home_gantry(self):
        threading.Thread(target=self.controller.hardware.home_gantry, daemon=True).start()

    def move_to_position(self, position):
        def move():
            try:
                self.controller.hardware.move_gantry_absolute(position)
            except Exception as e:
                logger.error(f"Move error: {e}")
        threading.Thread(target=move, daemon=True).start()

    def manual_move(self):
        try:
            distance = float(self.manual_move_entry.get())
            def move():
                try:
                    current = self.controller.hardware.get_gantry_position()
                    self.controller.hardware.move_gantry_absolute(current + distance)
                    self.manual_move_entry.delete(0, tk.END)
                except Exception as e:
                    logger.error(f"Manual move error: {e}")
            threading.Thread(target=move, daemon=True).start()
        except ValueError:
            messagebox.showerror("Error", "Invalid distance value")
            self.manual_move_entry.delete(0, tk.END)

    def run_automated(self):
        if messagebox.askyesno("Confirm", "Start automated operation? This will run the full cycle."):
            try:
                self.controller.start_operation()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start operation: {e}")

    def run_assay(self):
        try:
            self.controller.run_assay()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start assay: {e}")

    def classify_fly_gui(self):
        # Placeholder - implement fly classification
        messagebox.showinfo("Info", "Fly classification not yet implemented")

    def system_start(self):
        try:
            self.controller.start_operation()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start: {e}")

    def system_stop(self):
        self.controller.stop_operation()

    def system_reset(self):
        self.controller.reset_system()


if __name__ == "__main__":
    root = tk.Tk()
    app = DrosophilaGUI(root)
    root.mainloop()