from PIL import Image
from luma.core.interface.serial import spi
from luma.lcd.device import st7789

serial = spi(port=0, device=0, gpio_DC=25, gpio_RST=24, bus_speed_hz=40000000)
device = st7789(serial_interface=serial, width=240, height=240, rotate=3)

image = Image.open("assets/animations/idle.bmp").convert("RGB")
frame = image.crop((0, 0, 240, 240))  # First frame of column 0
device.display(frame)