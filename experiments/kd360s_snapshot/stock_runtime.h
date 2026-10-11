#ifndef SNAPSHOT_STOCK_RUNTIME_H
#define SNAPSHOT_STOCK_RUNTIME_H
#include <stdint.h>
#if !defined(__XTENSA_WINDOWED_ABI__) || !defined(__XTENSA_EL__)
#error "Requires Xtensa little-endian windowed ABI"
#endif
#ifndef COLLECTOR_IPV4_LE
#error "Configure the collector IPv4 address explicitly"
#endif
#ifndef COLLECTOR_PORT_BE
#error "Configure the collector UDP port explicitly"
#endif
_Static_assert(sizeof(void *) == 4, "Stock pointers are 32 bit");
#define IMPORT(address, result, ...) ((result (*)(__VA_ARGS__))(address))
#define stock_setup IMPORT(0x4200be5cu, void, void)
#define stock_loop IMPORT(0x4200cc98u, void, void)
#define stock_serial IMPORT(0x4200cc6cu, void, void)
#define watchdog_reset IMPORT(0x42073928u, int, void)
#define millis IMPORT(0x4206617cu, uint32_t, void)
#define delay IMPORT(0x42066198u, void, uint32_t)
#define allocate IMPORT(0x403846c8u, void *, uint32_t)
#define release IMPORT(0x403846e8u, void, void *)
#define fill_random IMPORT(0x42094a18u, void, void *, uint32_t)
#define create_task IMPORT(0x42066150u, int, void (*)(void *), const char *, uint32_t, void *, uint32_t, void *, int)
#define wifi_begin IMPORT(0x420561c0u, int, void *, const char *, const char *, int, const void *, int)
#define wifi_status IMPORT(0x420561a0u, int, void *)
#define udp_socket IMPORT(0x42082b44u, int, int, int, int)
#define udp_close IMPORT(0x420826b0u, int, int)
#define udp_send IMPORT(0x42082978u, int, int, const void *, uint32_t, int, const void *, uint32_t)
#define udp_receive IMPORT(0x42082884u, int, int, void *, uint32_t, int, void *, uint32_t *)
#define WIFI_OBJECT ((void *)0x3fca2128u)

static uint8_t read8(uint32_t address) { return *(volatile uint8_t *)address; }
static uint16_t read16(uint32_t address) { return *(volatile uint16_t *)address; }
static uint32_t read32(uint32_t address) { return *(volatile uint32_t *)address; }
static const char *string_address(uint32_t address) { return (const char *)address; }
#endif
