#import <Foundation/Foundation.h>
#import <IOBluetooth/IOBluetooth.h>
#import "bt_bridge.h"

static DataCallback globalCallback = NULL;

// 定義 Delegate 類別來處理 RFCOMM 事件
@interface BTDelegate : NSObject <IOBluetoothRFCOMMChannelDelegate>
@end

@implementation BTDelegate

- (void)rfcommChannelOpenComplete:(IOBluetoothRFCOMMChannel *)channel status:(IOReturn)status opID:(int)opID {
    if (status == kIOReturnSuccess) {
        printf(">>> [Mac] RFCOMM Channel 連線成功!\n");
    } else {
        printf(">>> [Mac] RFCOMM 連線失敗，錯誤碼: 0x%x\n", status);
        exit(1);
    }
}

- (void)rfcommChannelData:(IOBluetoothRFCOMMChannel *)channel data:(void *)dataPointer length:(size_t)dataLength {
    // 收到數據，轉傳給 Zig
    if (globalCallback) {
        globalCallback((const uint8_t*)dataPointer, dataLength);
    }
}

- (void)rfcommChannelClosed:(IOBluetoothRFCOMMChannel *)channel {
    printf(">>> [Mac] RFCOMM Channel 已關閉\n");
    exit(0);
}

@end

static volatile bool isRunning = true;

void stop_bluetooth_loop() {
    isRunning = false;
}

char* find_bluetooth_device_by_name(const char* target_name) {
    @autoreleasepool {
        NSString *target = [NSString stringWithUTF8String:target_name];
        NSArray *paired = [IOBluetoothDevice pairedDevices];

        // Exact match first
        for (IOBluetoothDevice *dev in paired) {
            NSString *name = [dev name];
            if (name && [name isEqualToString:target]) {
                NSString *addr = [dev addressString];
                if (addr) {
                    NSString *normalized = [addr stringByReplacingOccurrencesOfString:@"-" withString:@":"];
                    return strdup([normalized UTF8String]);
                }
            }
        }

        // Partial case-insensitive match
        for (IOBluetoothDevice *dev in paired) {
            NSString *name = [dev name];
            if (name && [name localizedCaseInsensitiveContainsString:target]) {
                NSString *addr = [dev addressString];
                if (addr) {
                    NSString *normalized = [addr stringByReplacingOccurrencesOfString:@"-" withString:@":"];
                    printf(">>> [Mac] Found device: %s (%s)\n", [name UTF8String], [normalized UTF8String]);
                    return strdup([normalized UTF8String]);
                }
            }
        }

        return NULL;
    }
}

void start_bluetooth_connection(const char* mac_addr, int channel_id, DataCallback callback) {
    globalCallback = callback;
    isRunning = true;

    @autoreleasepool {
        NSString *addrStr = [NSString stringWithUTF8String:mac_addr];
        IOBluetoothDevice *device = [IOBluetoothDevice deviceWithAddressString:addrStr];

        if (!device) {
            printf("錯誤: 找不到裝置 %s\n", mac_addr);
            return;
        }
        
        // 檢查是否已建立底層連線
        if (![device isConnected]) {
            printf(">>> [Mac] 底層未連線，強制執行 openConnection()...\n");
            
            // 1. 強制建立 Baseband 連線
            IOReturn ret = [device openConnection];
            
            if (ret != kIOReturnSuccess) {
                printf("錯誤: 底層連線失敗 (0x%x)\n", ret);
                // 這裡可以選擇是否要 return，或是硬著頭皮試試看
                return; 
            }
            
            printf(">>> [Mac] 底層連線建立，等待 1 秒讓訊號穩定...\n");
            
            // 2. 暫停 1 秒 (相當於 Python 的 time.sleep(1))
            [NSThread sleepForTimeInterval:1.0];
            
        } else {
            printf(">>> [Mac] 底層已連線 斷線重開...\n");
            // 如果已連線，先關閉再重新開啟
            [device closeConnection];
            [NSThread sleepForTimeInterval:1.0];
            IOReturn ret = [device openConnection];
            if (ret != kIOReturnSuccess) {
                printf("錯誤: 底層重新連線失敗 (0x%x)\n", ret);
                return;
            }
            printf(">>> [Mac] 底層重新連線建立，等待 1 秒讓訊號穩定...\n");
            [NSThread sleepForTimeInterval:1.0];
        }

        printf(">>> [Mac] 正在開啟 RFCOMM Channel %d...\n", channel_id);

        BTDelegate *delegate = [[BTDelegate alloc] init];
        IOBluetoothRFCOMMChannel *channel;
        
        // 發起 RFCOMM 連線
        [device openRFCOMMChannelAsync:&channel withChannelID:channel_id delegate:delegate];

        // 啟動 RunLoop (這會卡住這裡，直到程式結束)
        printf(">>> [Mac] 進入事件迴圈...\n");
        while (isRunning && [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.1]]) {
            // 這個迴圈每 0.1 秒會檢查一次 shouldKeepRunning
            // 或者當有藍牙事件發生時也會醒來
        }

        if ([device isConnected]) {
            // 關閉 RFCOMM (嚴格來說 closeConnection 會一併處理，但這樣比較保險)
            if (channel && [channel isOpen]) {
                [channel closeChannel];
            }
            
            // 斷開底層 Baseband 連線
            [device closeConnection];
            
            // 重要：給予藍牙 stack 一點時間送出斷線訊號
            [NSThread sleepForTimeInterval:0.5];
        }
    }
}

void cleanup_bluetooth_connection(const char* mac_addr) {
    @autoreleasepool{
        NSString *addrStr = [NSString stringWithUTF8String:mac_addr];
        IOBluetoothDevice *device = [IOBluetoothDevice deviceWithAddressString:addrStr];

        if([device isConnected]){
            printf(">>> [Mac] 清理連線: 關閉 RFCOMM Channel 和底層連線...\n");
            [device closeConnection];
        }
    }
}