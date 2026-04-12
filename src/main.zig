const std = @import("std");
const builtin = @import("builtin");
const c = @cImport({
    @cInclude("bt_bridge.h");
});

var stdout_file: std.fs.File = undefined;

var allocator = std.heap.page_allocator;

var should_exit = std.atomic.Value(bool).init(false);

var data_file: ?std.fs.File = null;
var write_to_file: bool = true;
var write_to_stdout: bool = false;

const default_device_name = "BlueZ 5.79";
const default_windows_addr: [:0]const u8 = "DC:EC:4F:5D:75:D8";

fn handle_signal(signum: c_int) callconv(.c) void {
    _ = signum;
    c.stop_bluetooth_loop();
}

fn setupSignalHandlers() void {
    if (comptime builtin.os.tag == .windows) {
        const windows = std.os.windows;
        _ = windows.kernel32.SetConsoleCtrlHandler(struct {
            fn handler(ctrl_type: windows.DWORD) callconv(.winapi) windows.BOOL {
                _ = ctrl_type;
                c.stop_bluetooth_loop();
                return windows.TRUE;
            }
        }.handler, windows.TRUE);
    } else {
        const act = std.posix.Sigaction{
            .handler = .{
                .handler = handle_signal,
            },
            .flags = 0,
            .mask = std.mem.zeroes(std.posix.sigset_t),
        };

        std.posix.sigaction(std.posix.SIG.INT, &act, null);
        std.posix.sigaction(std.posix.SIG.TERM, &act, null);
    }
}

export fn on_data_received(data: [*c]const u8, len: usize) void {
    if (data == null) {
        std.debug.print("Received null data pointer\n", .{});
        return;
    }
    if (len == 0) return;

    const slice = data[0..len];

    if (write_to_file) {
        if (data_file) |*f| {
            f.writeAll(slice) catch |err| {
                if (err == error.WouldBlock) {
                    // Retry once on WouldBlock
                    std.Thread.sleep(1 * std.time.ns_per_ms);
                    f.writeAll(slice) catch {};
                } else {
                    std.debug.print("Failed to write data to file: {any}\n", .{err});
                }
            };
        }
    }

    if (write_to_stdout) {
        stdout_file.writeAll(slice) catch {
            write_to_stdout = false;
            c.stop_bluetooth_loop();
            return;
        };
    }
}

pub fn main() !void {
    setupSignalHandlers();

    stdout_file = std.fs.File.stdout();

    var args = try std.process.argsWithAllocator(allocator);
    defer args.deinit();
    _ = args.next();

    var manual_addr: ?[:0]const u8 = null;
    var device_name: [:0]const u8 = default_device_name;

    while (args.next()) |arg| {
        if (std.mem.eql(u8, arg, "--stdout")) {
            write_to_stdout = true;
        } else if (std.mem.eql(u8, arg, "--no-file")) {
            write_to_file = false;
        } else if (std.mem.eql(u8, arg, "--addr")) {
            manual_addr = args.next();
        } else if (std.mem.eql(u8, arg, "--name")) {
            if (args.next()) |name| {
                device_name = name;
            }
        }
    }

    // Resolve Bluetooth address
    var bluetooth_addr: [*c]const u8 = undefined;
    var need_free = false;

    if (manual_addr) |addr| {
        bluetooth_addr = addr.ptr;
        std.debug.print(">>> Using manual address: {s}\n", .{addr});
    } else if (comptime builtin.os.tag == .macos) {
        std.debug.print(">>> Searching paired device \"{s}\"...\n", .{device_name});
        const found = c.find_bluetooth_device_by_name(device_name.ptr);
        if (found == null) {
            std.debug.print("Error: no paired device named \"{s}\"\n", .{device_name});
            std.debug.print("Pair the device in Mac Bluetooth settings first, or use --addr XX:XX:XX:XX:XX:XX\n", .{});
            return;
        }
        bluetooth_addr = found;
        need_free = true;
        const addr_slice = std.mem.span(found);
        std.debug.print(">>> Auto-detected device: {s}\n", .{addr_slice});
    } else if (comptime builtin.os.tag == .windows) {
        bluetooth_addr = default_windows_addr.ptr;
        std.debug.print(">>> [Win] Using default address: {s} (override with --addr)\n", .{default_windows_addr});
    } else {
        @compileError("Unsupported target OS");
    }
    defer if (need_free) std.c.free(@ptrCast(@constCast(bluetooth_addr)));

    if (write_to_file) {
        data_file = try std.fs.cwd().createFile("Raw_data.csv", .{
            .read = false,
            .truncate = false,
        });

        // Move to the end of the file to append new data
        try data_file.?.seekFromEnd(0);
    }
    defer if (data_file) |f| f.close();

    c.start_bluetooth_connection(bluetooth_addr, 1, on_data_received);
}
