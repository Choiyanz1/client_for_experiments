#include <winsock2.h>
#include <ws2bth.h>
#include <stdio.h>
#include <stdbool.h>
#include <windows.h>
#include "bt_bridge.h"

static volatile bool isRunning = false;
static SOCKET bt_socket = INVALID_SOCKET;

void stop_bluetooth_loop() {
    isRunning = false;
    if (bt_socket != INVALID_SOCKET) {
        closesocket(bt_socket);
    }
}

static BTH_ADDR parse_mac_address(const char *mac_str) {
    unsigned int b[6];
    sscanf(mac_str, "%02X:%02X:%02X:%02X:%02X:%02X",
           &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]);
    return ((BTH_ADDR)b[0] << 40) |
           ((BTH_ADDR)b[1] << 32) |
           ((BTH_ADDR)b[2] << 24) |
           ((BTH_ADDR)b[3] << 16) |
           ((BTH_ADDR)b[4] << 8)  |
           ((BTH_ADDR)b[5]);
}

void start_bluetooth_connection(const char *mac_addr, int channel_id, DataCallback callback) {
    WSADATA wsaData;
    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
        printf(">>> [Win] WSAStartup 失敗\n");
        return;
    }

    isRunning = true;

    SOCKADDR_BTH sa = {0};
    sa.addressFamily = AF_BTH;
    sa.btAddr = parse_mac_address(mac_addr);
    sa.port = (ULONG)channel_id;

    for (int attempt = 1; attempt <= 5 && isRunning; attempt++) {
        printf(">>> [Win] 建立連線... (第 %d 次)\n", attempt);

        bt_socket = socket(AF_BTH, SOCK_STREAM, BTHPROTO_RFCOMM);
        if (bt_socket == INVALID_SOCKET) {
            printf(">>> [Win] 無法建立 socket: %d\n", WSAGetLastError());
            break;
        }

        if (connect(bt_socket, (SOCKADDR *)&sa, sizeof(sa)) == 0) {
            printf(">>> [Win] RFCOMM Channel 連線成功!\n");
            break;
        }

        printf(">>> [Win] 連線失敗 (%d)，等待 2 秒後重試...\n", WSAGetLastError());
        closesocket(bt_socket);
        bt_socket = INVALID_SOCKET;
        Sleep(2000);
    }

    if (!isRunning) {
        printf(">>> [Win] 收到中斷信號\n");
        WSACleanup();
        return;
    }

    if (bt_socket == INVALID_SOCKET) {
        printf("錯誤: 連線重試 5 次仍失敗\n");
        WSACleanup();
        return;
    }

    uint8_t buf[4096];
    while (isRunning) {
        int received = recv(bt_socket, (char *)buf, sizeof(buf), 0);
        if (received <= 0) {
            if (isRunning) {
                printf(">>> [Win] 連線已斷開 (recv=%d, err=%d)\n", received, WSAGetLastError());
            }
            break;
        }
        if (callback) {
            callback(buf, (size_t)received);
        }
    }

    if (bt_socket != INVALID_SOCKET) {
        closesocket(bt_socket);
        bt_socket = INVALID_SOCKET;
    }
    printf(">>> [Win] 連線已關閉\n");
    WSACleanup();
}

void cleanup_bluetooth_connection(const char *mac_addr) {
    (void)mac_addr;
    stop_bluetooth_loop();
}
