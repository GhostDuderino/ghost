# display.py
import os, threading
SIM = os.getenv("GHOST_SIM") == "1"

if SIM:
    import pygame
    _MAIN_IDENT = threading.get_ident()

    class _PygDisplay:
        def __init__(self, w=240, h=240, title="GHOST SIM"):
            # All pygame init must be on the main thread
            assert threading.get_ident() == _MAIN_IDENT, "pygame must init on main thread"
            pygame.init()
            pygame.display.set_caption(title)
            self.size = (w, h)
            self.screen = pygame.display.set_mode(self.size)

        def display(self, pil_image):
            # All pygame calls must be on the main thread
            assert threading.get_ident() == _MAIN_IDENT, "pygame must be used from main thread"
            # PIL → pygame surface, blit, flip
            mode = pil_image.mode  # e.g., "RGB"
            data = pil_image.tobytes()
            surf = pygame.image.fromstring(data, pil_image.size, mode)
            if pil_image.size != self.size:
                surf = pygame.transform.scale(surf, self.size)
            self.screen.blit(surf, (0, 0))
            pygame.display.flip()

    def init_display():
        # Creates the window on the main thread
        return _PygDisplay(240, 240)

    def show_image(device, pil_image):
        device.display(pil_image)

else:
    # Real device (Pi) — leave as-is
    from luma.core.interface.serial import spi
    from luma.lcd.device import st7789

    def init_display():
        serial = spi(port=0, device=0, gpio=None)
        return st7789(serial, width=240, height=240, rotate=3)

    def show_image(device, pil_image):
        device.display(pil_image)