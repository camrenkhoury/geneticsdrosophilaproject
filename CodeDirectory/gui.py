#!/usr/bin/env python3
"""
GUI for Drosophila Genetics Project
Provides a graphical interface to control the gantry, vacuum, vibration, and assays.
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import time
import queue

# Import project modules
from motion import home_to_zero, move_to_absolute, get_current_position, move_relative, GPIO_AVAILABLE
from vacuum import vacuum_on, vacuum_off
from vibration import vibration_on, vibration_off
from assay import assay
from nozzle_implementation import run_operation_non_interactive
from fly_classifier import classify_fly
import config


class DrosophilaGUI:
    def __init__(self, root):
        self.root = root
        mode = " (Simulation Mode)" if not GPIO_AVAILABLE else ""
        self.root.title(f"Drosophila Genetics Control Panel{mode}")
        self.root.geometry("800x600")

        # Queue for thread communication
        self.queue = queue.Queue()

        # Status variables
        self.current_position = tk.StringVar(value="0.00 mm")
        self.status_text = tk.StringVar(value="Ready")

        self.create_widgets()
        self.update_position()
        self.process_queue()

    def create_widgets(self):
        # Main frame
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Configure grid weights
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(4, weight=1)

        # Title
        title_label = ttk.Label(main_frame, text="Drosophila Genetics Control Panel",
                               font=("Arial", 16, "bold"))
        title_label.grid(row=0, column=0, columnspan=3, pady=(0, 10))

        # Status section
        status_frame = ttk.LabelFrame(main_frame, text="Status", padding="5")
        status_frame.grid(row=1, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))

        ttk.Label(status_frame, text="Current Position:").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(status_frame, textvariable=self.current_position).grid(row=0, column=1, sticky=tk.W)

        ttk.Label(status_frame, text="Status:").grid(row=1, column=0, sticky=tk.W)
        ttk.Label(status_frame, textvariable=self.status_text).grid(row=1, column=1, sticky=tk.W)

        mode_text = "Hardware Mode" if GPIO_AVAILABLE else "Simulation Mode"
        ttk.Label(status_frame, text="Mode:").grid(row=2, column=0, sticky=tk.W)
        ttk.Label(status_frame, text=mode_text).grid(row=2, column=1, sticky=tk.W)

        # Motion control section
        motion_frame = ttk.LabelFrame(main_frame, text="Motion Control", padding="5")
        motion_frame.grid(row=2, column=0, sticky=(tk.W, tk.E, tk.N), padx=(0, 5))

        ttk.Button(motion_frame, text="Home", command=self.home_motor).grid(row=0, column=0, pady=2)
        ttk.Button(motion_frame, text="Move to Channel", command=lambda: self.move_to_position(config.CHANNEL_CENTER)).grid(row=1, column=0, pady=2)
        ttk.Button(motion_frame, text="Move to Chamber", command=lambda: self.move_to_position(config.CHAMBER_CENTER)).grid(row=2, column=0, pady=2)
        ttk.Button(motion_frame, text="Move to Tube 1", command=lambda: self.move_to_position(config.TUBE_1_CENTER)).grid(row=3, column=0, pady=2)
        ttk.Button(motion_frame, text="Move to Tube 2", command=lambda: self.move_to_position(config.TUBE_2_CENTER)).grid(row=4, column=0, pady=2)
        ttk.Button(motion_frame, text="Move to Tube 3", command=lambda: self.move_to_position(config.TUBE_3_CENTER)).grid(row=5, column=0, pady=2)
        ttk.Button(motion_frame, text="Move to Tube 4", command=lambda: self.move_to_position(config.TUBE_4_CENTER)).grid(row=6, column=0, pady=2)
        ttk.Button(motion_frame, text="Move to Tube 5", command=lambda: self.move_to_position(config.TUBE_5_CENTER)).grid(row=7, column=0, pady=2)

        # Manual move
        ttk.Label(motion_frame, text="Manual Move (mm):").grid(row=8, column=0, pady=(10, 2))
        self.manual_move_entry = ttk.Entry(motion_frame, width=10)
        self.manual_move_entry.grid(row=9, column=0, pady=2)
        ttk.Button(motion_frame, text="Move Relative", command=self.manual_move).grid(row=10, column=0, pady=2)

        # Device control section
        device_frame = ttk.LabelFrame(main_frame, text="Device Control", padding="5")
        device_frame.grid(row=2, column=1, sticky=(tk.W, tk.E, tk.N), padx=(5, 5))

        ttk.Button(device_frame, text="Vacuum ON", command=lambda: self.run_threaded(vacuum_on)).grid(row=0, column=0, pady=2)
        ttk.Button(device_frame, text="Vacuum OFF", command=lambda: self.run_threaded(vacuum_off)).grid(row=0, column=1, pady=2)
        ttk.Button(device_frame, text="Vibration ON", command=lambda: self.run_threaded(vibration_on)).grid(row=1, column=0, pady=2)
        ttk.Button(device_frame, text="Vibration OFF", command=lambda: self.run_threaded(vibration_off)).grid(row=1, column=1, pady=2)

        # Operations section
        ops_frame = ttk.LabelFrame(main_frame, text="Operations", padding="5")
        ops_frame.grid(row=2, column=2, sticky=(tk.W, tk.E, tk.N))

        ttk.Button(ops_frame, text="Run Automated Operation", command=self.run_automated).grid(row=0, column=0, pady=2)
        ttk.Button(ops_frame, text="Run Assay", command=self.run_assay).grid(row=1, column=0, pady=2)
        ttk.Button(ops_frame, text="Classify Fly", command=self.classify_fly_gui).grid(row=2, column=0, pady=2)

        # Log section
        log_frame = ttk.LabelFrame(main_frame, text="Log", padding="5")
        log_frame.grid(row=3, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.S), pady=(10, 0))

        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, wrap=tk.WORD)
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.S))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

    def update_position(self):
        try:
            pos = get_current_position()
            self.current_position.set(f"{pos:.2f} mm")
        except Exception as e:
            self.log_message(f"Error getting position: {e}")
        self.root.after(1000, self.update_position)  # Update every second

    def process_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                if msg.startswith("STATUS:"):
                    self.status_text.set(msg[7:])
                elif msg.startswith("LOG:"):
                    self.log_message(msg[4:])
        except queue.Empty:
            pass
        self.root.after(100, self.process_queue)

    def log_message(self, message):
        self.log_text.insert(tk.END, f"{time.strftime('%H:%M:%S')} - {message}\n")
        self.log_text.see(tk.END)

    def run_threaded(self, func, *args):
        def wrapper():
            try:
                self.queue.put("STATUS:Busy")
                func(*args)
                self.queue.put("STATUS:Ready")
            except Exception as e:
                self.queue.put(f"LOG:Error in {func.__name__}: {e}")
                self.queue.put("STATUS:Error")

        threading.Thread(target=wrapper, daemon=True).start()

    def home_motor(self):
        self.run_threaded(home_to_zero)

    def move_to_position(self, position):
        self.run_threaded(move_to_absolute, position)

    def manual_move(self):
        try:
            distance = float(self.manual_move_entry.get())
            self.run_threaded(move_relative, distance)
        except ValueError:
            messagebox.showerror("Error", "Invalid distance value")

    def run_automated(self):
        if messagebox.askyesno("Confirm", "Start automated operation? This will run the full cycle."):
            self.run_threaded(run_operation_non_interactive)

    def run_assay(self):
        self.run_threaded(assay)

    def classify_fly_gui(self):
        def classify():
            try:
                result = classify_fly()
                self.queue.put(f"LOG:Classification result: {result}")
            except Exception as e:
                self.queue.put(f"LOG:Classification error: {e}")

        self.run_threaded(classify)


if __name__ == "__main__":
    root = tk.Tk()
    app = DrosophilaGUI(root)
    root.mainloop()