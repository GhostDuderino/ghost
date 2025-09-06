# ghost/hw/buttons.py
import os

SIM = os.getenv("GHOST_SIM") == "1"

if SIM:
    import pygame

    _inited = False

    def _ensure_inited():
        """Make sure pygame's event system is ready. Must be called on the main thread."""
        global _inited
        if not _inited:
            # display.py creates the window; we just ensure event subsystem is ready.
            try:
                pygame.event.pump()
            except Exception:
                pass
            _inited = True

    # Keybindings (SIM only)
    KEYMAP = {
        pygame.K_1: "B1_TAP",
        pygame.K_2: "B1_DOUBLE",
        pygame.K_3: "B1_HOLD",
        pygame.K_4: "B2_TAP",
        pygame.K_5: "B2_DOUBLE",
        pygame.K_6: "B2_HOLD",
        pygame.K_SPACE: "B1B2_CHORD",
    }

    def poll_buttons():
        """
        SIM: Called from the MAIN THREAD only (via pump_sim_inputs_once()).
        Returns a dict like {"B1_TAP": True, ...} for any events pressed this tick.
        """
        _ensure_inited()

        events = {}

        # Pump + drain event queue
        try:
            pygame.event.pump()
        except Exception:
            # If the window isn't ready yet, just return nothing this tick
            return events

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                events["QUIT"] = True
            elif ev.type == pygame.KEYDOWN:
                name = KEYMAP.get(ev.key)
                if name:
                    events[name] = True

        return events

else:
    # On the Pi, GPIO edges are handled in ghost.ghost:input_monitor().
    # This function simply returns no simulated keyboard events.
    def poll_buttons():
        return {}