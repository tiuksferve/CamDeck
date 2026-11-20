idf_component_register(SRCS "main.c" "link.c" "cam.c" "mic.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_driver_usb_serial_jtag esp_driver_i2s esp_driver_tsens esp_timer json esp_rom)
