"""Unified stitch-style operator application for the drosophila rig."""


def main():
    from .app import main as run_main

    return run_main()


__all__ = ["main"]
