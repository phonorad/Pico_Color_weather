import network
import urequests
import time
import machine
from machine import Pin, SPI
import math
import ntptime
import gc
import uio
import sys
import uasyncio
import hashlib
from phew import access_point, connect_to_wifi, is_connected_to_wifi, dns, server
from phew.template import render_template
from phew import logging
from phew.server import Response
import ujson as json
import os
import ure  # MicroPython’s regex module
import _thread

# Imports for round color tft display
import gc9a01py as gc9a01
import vga1_8x16 as font_sm
import vga1_16x16 as font_lg
import vga1_16x32 as font_huge

# === Software Version ===
__version__ = "1.0.0"
# ========================

# === Definitons for Wifi Setup and Access ===
AP_NAME = "P&L Forecaster"
AP_DOMAIN = "plforecaster.net"
AP_TEMPLATE_PATH = "ap_templates"
APP_TEMPLATE_PATH = "app_templates"
SETTINGS_FILE = "settings.json"
WIFI_MAX_ATTEMPTS = 3

# === Initialize/define parameters ===
SYNC_INTERVAL = 3600 # Sync to NTP time server every hour
WEATH_INTERVAL = 1800 # Update forecast every 30 mins
last_sync = 0
last_weather_update = 0
press_time = None
long_press_triggered = False
start_update_requested = False
# continue_requested = False
init_complete = False      # Indicate whether all init is completed (lat lon, gmt offset, weather)
gmt_offset_complete = False
lat_lon_complete = False
weath_setup_complete = False
client_connected = False
sunrise = None
sunset = None
last_sun_fetch_day = None
last_displayed_time = ""
last_displayed_date = ""
last_sun_update_date = None  # track date of last sunrise/sunset fetch
sunrise_sunset_data = None   # to store sunrise/sunset info
retry_allowed = True  # flag whether to allow lat/lon lookup retry or not

UPLOAD_TEMP_SUFFIX = ".tmp"

# === Need this for NWS Weather API ====
USER_AGENT = "PLForecastDisplay (phonorad@gmail.com)"  # replace with your info

# === Define Months ===
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# === Define US Timezones ===
# Standard U.S. timezones without DST applied yet
TIMEZONE_OFFSETS = {
    "Eastern": -5,
    "Central": -6,
    "Mountain": -7,
    "Pacific": -8,
    "Alaska": -9,
    "Hawaii": -10,
    "Manual": None  # will be handled separately
}

# === Define timezone ===
gmt_offset = 0   # Initialze gmt offset

# === SPI and Display Init ===
WIDTH = 240
HEIGHT = 240
spi = SPI(1, baudrate=40000000, polarity=1, phase=1, sck=Pin(10), mosi=Pin(11))
display = gc9a01.GC9A01(
    spi,
    dc=Pin(8, Pin.OUT),
    cs=Pin(9, Pin.OUT),
    reset=Pin(12, Pin.OUT)
)

# === Color helper ===
def color565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    
# === Other GPIO Setup ===
onboard_led = machine.Pin("LED", machine.Pin.OUT)
setup_sw = machine.Pin(5, machine.Pin.IN, machine.Pin.PULL_UP)

# === Memory usage monitor function - call this to print memory usage ====
def print_memory_usage():
    free = gc.mem_free()
    allocated = gc.mem_alloc()
    total = free + allocated
    print("RAM usage:")
    print(f"  Free:      {free} bytes")
    print(f"  Allocated: {allocated} bytes")
    print(f"  Total:     {total} bytes\n")
    
def test_free_memory(max_size=60000, step=1024):
    gc.collect()  # force garbage collection
    print("Testing max allocatable memory in bytes...")

    size = step
    last_good = 0

    while size <= max_size:
        try:
            _ = bytearray(size)
            last_good = size
            size += step
        except MemoryError:
            break

    print("Max allocatable buffer size:", last_good, "bytes")
    return last_good

def safe_mkdirs(path):
    parts = path.split("/")
    current = ""
    for part in parts:
        if not part:
            continue
        current = current + "/" + part if current else part
        try:
            os.mkdir(current)
        except OSError:
            pass  # Directory exists

# === AP and Wi-Fi Setup ===
def load_settings():
    # Check if file is missing
    if SETTINGS_FILE not in os.listdir():
        print("Settings file is missing.")
        return "missing", None, "No Settings Found"

    try:
        # Try to parse JSON
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)

        # Validate WiFi SSID (Required Parameter)
        ssid = settings.get("ssid", "").strip()
        if not ssid:
            print("SSID missing from settings")
            return "invalid", None, "WiFi SSID missing"
        
        # Password can be empty (open networks allowed)
        password = settings.get("password", "")
        if password == "":
            print("Note: Wi-Fi password not set (open network)")
        
                # Validate ZIP or lat/lon - require at least one valid set
        zip_code = settings.get("zip", "").strip()
        lat_raw = settings.get("lat")
        lon_raw = settings.get("lon")

        lat = None
        lon = None
        latlon_valid = False

        # Validate lat/lon if both are present and not empty strings
        if lat_raw not in [None, ""] and lon_raw not in [None, ""]:
            try:
                lat = float(lat_raw)
                lon = float(lon_raw)
                latlon_valid = True
                print(f"Settings loaded with lat/lon: {lat}, {lon}")
            except ValueError:
                print("Invalid settings: lat/lon must be numbers")

        if zip_code:
            print(f"Settings loaded with ZIP code: {zip_code}")
        elif latlon_valid:
            pass  # Already logged above
        else:
            print("Invalid settings: Must provide ZIP code or valid lat/lon")
            return "invalid", None, "Bad ZIP or lat/lon"
        
        # Validate timezone info
        tz = settings.get("timezone")
        if not tz:
            print("Invalid settings: Missing timezone")
            return "invalid", None, "Timezone missing"

        if tz == "manual":
            try:
                mo = settings.get("manual_offset", "")
                if mo == "":
                    print("Invalid settings: manual_offset not provided")
                    return "invalid", None, "Need Timezone offset"
                float(mo)  # Just check it's a valid float
            except ValueError:
                print("Invalid settings: Timezone offset not a number")
                return "invalid", None, "Timezone offset not a number"

        print(f"Timezone loaded: {tz}, DST enabled: {settings.get('use_dst')}, manual_offset: {settings.get('manual_offset')}")

        return "valid", settings, "Settings valid"

    except Exception as e:

        buf = uio.StringIO()
        sys.print_exception(e, buf)
#        logging.exception("Settings file error:\n" + buf.getvalue())

        return "corrupt", None, "Settings file corrupt"

def save_settings(settings):
    """
    Save the settings dictionary to the settings.json file.
    Overwrites the file with new data.
    """
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f)
        print("Settings saved successfully.")
        return True
    except Exception as e:
        print("Failed to save settings:", e)
        return False

def serve_config_page(setup_mode: bool):
    def response_gen():
        with open(f"{AP_TEMPLATE_PATH}/config_settings.html", "r") as f:
            for line in f:
                yield line.replace(
                    "window.setupMode = false;",
                    f"window.setupMode = {str(setup_mode).lower()};"
                )
    return Response(response_gen(), status=200, headers={"Content-Type": "text/html"})

def machine_reset():
    time.sleep(2)
    print("Rebooting...")
    machine.reset()

def setup_mode():
    print("Entering setup mode...")
    display.fill(color565(0, 0, 0))
    center_lgtext("Setup Mode",40, color565(0, 255, 0))
    center_smtext("On Phone or Computer", 80)
    center_smtext("Open WiFi/Network settings", 100)
    center_smtext("and select network:", 120)
    center_lgtext("P&L Forecaster", 140, color565(255, 255, 0))
    
    def load_settings():
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            # Defaults if file missing or corrupt
            return {
                "location_source": "zip",
                "zip": "",
                "lat": "",
                "lon": "",
                "timezone": "",
                "use_dst": False,
                "manual_offset": ""
            }

    def ap_index(request):
        global client_connected
        if not client_connected:
            print("Client browser contacted /")
            display.fill(color565(0, 0, 0))
            center_lgtext("WiFi", 40, color565(0, 255, 0))
            center_lgtext("Connected!", 60, color565(0, 255, 0))
            center_smtext("Opening Config Page...", 100)
            center_smtext(f"If page does not load,", 120)
            center_smtext(f"open browser to:", 140)
            center_smtext(f"http://{AP_DOMAIN}", 160, color565(255, 255, 0))
            client_connected = True
            
        # Redirect if host header is not the expected AP domain
        if request.headers.get("host").lower() != AP_DOMAIN.lower():
            return render_template(f"{AP_TEMPLATE_PATH}/redirect.html", domain = AP_DOMAIN.lower())
        
        # setup_mode=True means show WiFi fields, hide software update
        return serve_config_page(setup_mode=True)

#    def ap_configure(request):
#        print("Saving wifi, location and timezone settings...")

#        with open(SETTINGS_FILE, "w") as f:
#            json.dump(request.form, f)
            
#        display.fill(color565(0, 0, 0))
#        center_lgtext("Settings", 60, color565(0, 255, 0))
#        center_lgtext("Saved!", 80, color565(0, 255, 0))
#        center_smtext("Restarting...", 120)
            

#        # Define a generator response so we can delay and reset after sending the page
#        def response_gen():
#            # Yield HTML content to the browser using a generator
#            yield from render_template(f"{AP_TEMPLATE_PATH}/configured.html")
    
#            # Flush and pause to the display updates
#            time.sleep(2)
    
#            # Reboot the device (this line will only run after page is fully sent)
#            print("Settings save, rebooting now...")
#            machine.reset()

#        # Return the generator to the server — Phew will stream the response
#        return response_gen()
    
    def load_settings():
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {
                "location_source": "zip",
                "zip": "",
                "lat": "",
                "lon": "",
                "timezone": "",
                "use_dst": False,
                "manual_offset": ""
            }

    def settings_get_handler(request):
        print("GET /settings received (setup mode)")
        settings = load_settings()
        return Response(json.dumps(settings), status=200, headers={"Content-Type": "application/json"})

    def settings_post_handler(request):
        try:
            form = request.form
            current_settings = load_settings()
            current_settings.update({
                "location_source": form.get("location_source", "zip"),
                "zip": form.get("zip", "").strip(),
                "lat": form.get("lat", "").strip(),
                "lon": form.get("lon", "").strip(),
                "timezone": form.get("timezone", ""),
                "use_dst": form.get("use_dst") in ("true", "on", "1"),
                "manual_offset": form.get("manual_offset", ""),
                "ssid": form.get("ssid", "").strip(),
                "password": form.get("password", "").strip()
            })

            with open(SETTINGS_FILE, "w") as f:
                json.dump(current_settings, f)

            # Feedback on OLED
            display.fill(color565(0, 0, 0))
            center_lgtext("Settings", 60, color565(0, 255, 0))
            center_lgtext("Saved!", 80, color565(0, 255, 0))
            center_smtext("Restarting...", 120)

            def response_gen():
                yield from render_template(f"{AP_TEMPLATE_PATH}/configured.html")
                time.sleep(2)
                machine.reset()

            return response_gen()

        except Exception as e:
            return Response(f"Failed to save settings: {e}", status=500)
    
    def ap_catch_all(request):
        if request.headers.get("host") != AP_DOMAIN:
            return render_template(f"{AP_TEMPLATE_PATH}/redirect.html", domain = AP_DOMAIN)

        return "Not found.", 404

    server.add_route("/", handler = ap_index, methods = ["GET"])
#    server.add_route("/configure", handler = ap_configure, methods = ["POST"])
    server.add_route("/settings", handler=settings_get_handler, methods=["GET"])
    server.add_route("/settings", handler=settings_post_handler, methods=["POST"])
    server.set_callback(ap_catch_all)

    ap = access_point(AP_NAME)
    ip = ap.ifconfig()[0]
    dns.run_catchall(ip)

def start_update_mode():
    print("starting update mode")
    
    expected_checksums = {}
    
    ip = network.WLAN(network.STA_IF).ifconfig()[0]
    print(f"start_update_mode: got IP = {ip}")
    
    display.fill(color565(0, 0, 0))
    center_lgtext("Software",60,color565(0, 255, 0))
    center_lgtext("Update Mode",80,color565(0, 255, 0))
    center_smtext("Enter", 100)
    center_smtext(f"http://{ip}", 120,color565(255, 255, 0))
    center_smtext("into browser", 140)

    def ap_version(request):
        # Return the version defined in main.py
        return Response(__version__, status=200, headers={"Content-Type": "text/plain"})

    def swup_handler(request):
        # Serve your software update HTML page here
        return serve_config_page(setup_mode=False)

    def favicon_handler(request):
        return Response("", status=204)  # No Content
    
    async def checksums_handler(request):
        nonlocal expected_checksums
        try:
            body = await request.read()
            expected_checksums = json.loads(body)
            return Response("Checksums received", status=200)
        except Exception as e:
            return Response(f"Error reading checksums: {e}", status=400)

    async def finalize_handler(request):
        failed = []
        # Validate checksums
        for filename, expected_hash in expected_checksums.items():
            try:
                with open(filename, "rb") as f:
                    sha = hashlib.sha256()
                    while True:
                        chunk = f.read(1024)
                        if not chunk:
                            break
                        sha.update(chunk)
                    actual_hash = sha.hexdigest()
                    if actual_hash != expected_hash:
                        failed.append((filename, "Checksum mismatch"))
            except Exception as e:
                failed.append((filename, str(e)))

        if failed:
            for filename in expected_checksums:
                try:
                    os.remove(filename)
                except:
                    pass
            return Response("Update failed:\n" + "\n".join([f"{f}: {reason}" for f, reason in failed]), status=500)
        
        # Rename all .new files except main_app.py.new
        for filename in expected_checksums:
            if filename.endswith(".new") and filename != "main_app.py.new":
                final_name = filename[:-4]
                try:
                    os.remove(final_name) # Safe overwrite
                except:
                    pass
                try:
                    os.rename(filename, final_name)
                except Exception as e:
                    return Response(f"Rename failed: {filename} -> {final_name}: {e}", status=500)

        # Optional OLED display
        display.fill(color565(0, 0, 0))
        center_lgtext("Update", 60, color565(0, 255, 0))
        center_lgtext("Complete!", 80, color565(0, 255, 0))
        center_smtext("Rebooting on OK", 100)

        return Response("Update verified and applied", status=200)

    def continue_handler(request):
#        global continue_requested
#        continue_requested = True
        print("Software updated, OK clicked, restarting device...")

        # Display restarting received message    
        display.fill(color565(0, 0, 0))
        center_lgtext("New Version", 60, color565(0, 255, 0))
        center_lgtext("Saved!", 80, color565(0, 255, 0))
        center_smtext("Restarting device...", 120)
    
        return render_template(f"{AP_TEMPLATE_PATH}/update_complete.html")
#        def response_gen():
#            yield "OK"
#            yield from render_template(f"{AP_TEMPLATE_PATH}/update_complete.html")
#            time.sleep(2)
#            print("Software update complete. Rebooting now...")
#            machine.reset()

#         return response_gen()
    
    def update_complete_handler(request):
        print("Serving update_complete.html")
        return render_template(f"{AP_TEMPLATE_PATH}/update_complete.html")
    
    async def upload_handler(request):
#        filename = request.query.get("filename")
        print("Entered upload_handler()")
        filepath = request.query.get("path") or request.query.get("filename")
        if not filepath:
            print("[UPLOAD] Missing path")
            return Response("Missing path", status=400)
        
        # Sanitize and normalize
        if ".." in filepath or filepath.startswith("/") or "\\" in filepath:
            print(f"[UPLOAD] Invalid path: {filepath}")
            return Response("Invalid path", status=400)

        # Ensure parent directory exists
        try:
            dir_path = "/".join(filepath.split("/")[:-1])
            if dir_path:
                print(f"[UPLOAD] Ensuring directory: {dir_path}")
                safe_mkdirs(dir_path)
        except Exception as e:
            print(f"[UPLOAD] Failed to create folders: {e}")
            return Response(f"Failed to create folders for {filepath}: {e}", status=500)
    
        try:
            print(f"[UPLOAD] Starting write to {filepath}")
            total_written = 0
            chunk_size = 1024

            with open(filepath, "wb") as f:
                while True:
                    chunk = await request.read_body_chunk(chunk_size)
                    if chunk is None:
                        await uasyncio.sleep(0.05)
                        continue
                    if chunk == b'':
                        # EOF: end of upload
                        print("[UPLOAD] Received EOF")
                        break
                    f.write(chunk)
                    total_written += len(chunk)
                    print(f"[UPLOAD] Wrote chunk of {len(chunk)} bytes (total so far: {total_written})")
            
            print(f"[UPLOAD] Finished writing {total_written} bytes to {filepath}")
            
            # Display file received message    
            display.fill(color565(0, 0, 0))
            center_lgtext("New Version", 60, color565(0, 255, 0))
            center_lgtext("Received!", 80, color565(0, 255, 0))
            center_smtext(f"{total_written}B to ", 100)
            center_smtext(filepath, 120)
            center_smtext("Click OK in browser", 140)

            return Response(f"Saved {total_written} bytes to {filepath}", status=200)

        except Exception as e:
            print(f"[UPLOAD] Exception while writing to {filepath}: {e}")
            return Response(f"Error writing to {filepath}: {e}", status=500)
        
    def load_settings():
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            # Defaults if file missing or corrupt
            return {
            "location_source": "zip",
            "zip": "",
            "lat": "",
            "lon": "",
            "timezone": "",
            "use_dst": False,
            "manual_offset": ""
        }

    def json_response(data, status=200):
        body = json.dumps(data)
        return Response(
            body,
            status=status,
            headers={"Content-Type": "application/json"}
        )

    def settings_get_handler(request):
        print("GET /settings received")
        settings = load_settings()
        print("Sending settings to browser:", settings)
        return Response(json.dumps(settings), status=200, headers={"Content-Type": "application/json"})

    def settings_post_handler(request):
        try:
            form = request.form
            
            # Load existing settings first
            current_settings = load_settings()
            
            # Only update the fields from the form
            current_settings.update({
                "location_source": form.get("location_source", "zip"),
                "zip": form.get("zip", "").strip(),
                "lat": form.get("lat", "").strip(),
                "lon": form.get("lon", "").strip(),
                "timezone": form.get("timezone", ""),
                "use_dst": form.get("use_dst") in ("true", "on", "1"),
                "manual_offset": form.get("manual_offset", ""),
            })
            
            # Save merged settings
            with open(SETTINGS_FILE, "w") as f:
                json.dump(current_settings, f)
                
            # Show success and reboot
            display.fill(color565(0, 0, 0))
            center_lgtext("Settings", 60, color565(0, 255, 0))
            center_lgtext("Saved!", 80, color565(0, 255, 0))
            center_smtext("Restarting...", 120)
            
            def response_gen():
                yield from render_template(f"{AP_TEMPLATE_PATH}/configured.html")
                time.sleep(2)
                print("Settings updated, rebooting now...")
                machine.reset()

            return response_gen()
            
        except Exception as e:
            return Response(f"Failed to save settings: {e}", status=500)
        
    def reboot_handler(request):
        print("Rebooting now...")
        machine.reset()
        return Response("Rebooting...", status=200)
        
    def catch_all_handler(request):
        print(f"Fallback route hit: {request.method} {request.path}")
        return Response("Route not found", status=404)
        
    server.add_route("/", handler=swup_handler, methods=["GET"])
    server.add_route("/settings", handler=settings_get_handler, methods=["GET"])
    server.add_route("/settings", handler=settings_post_handler, methods=["POST"])
    server.add_route("/version", handler=ap_version, methods=["GET"])
    server.add_route("/favicon.ico", handler=favicon_handler, methods=["GET"])
    server.add_route("/continue", handler=continue_handler, methods=["POST"])
    server.add_route("/upload", handler=upload_handler, methods=["POST"])
    server.add_route("/checksums", handler=checksums_handler, methods=["POST"])
    server.add_route("/finalize", handler=finalize_handler, methods=["POST"])
    server.add_route("/update_complete.html", handler=update_complete_handler, methods=["GET"])
    server.add_route("/reboot", reboot_handler, methods=["POST"])
        
    # Start the server (if not already running)
    print(f"Waiting for user at http://{ip} ...")
    server.run()

    # Wait until user clicks OK
#    while not continue_requested:
#        time.sleep(0.1)

# === Handler for button presses during operation ===
def setup_sw_handler(pin):
    global press_time, long_press_triggered, start_update_requested
    if pin.value() == 0:  # Falling edge: button pressed
        press_time = time.ticks_ms()
        long_press_triggered = False
    else:  # Rising edge: button released
        if press_time is not None:
            duration = time.ticks_diff(time.ticks_ms(), press_time)
            if duration >= 5000:  # 5 seconds
                long_press_triggered = True
                print("Long press detected!")
                # Set flag for main loop to poll and to call start_update_mode
                start_update_requested = True
            press_time = None
# Set up input as irq triggered, falling edge            
setup_sw.irq(trigger=machine.Pin.IRQ_FALLING | machine.Pin.IRQ_RISING, handler=setup_sw_handler)

# === Time Funcitions =====
def sync_time(max_retries=3, delay=3):
    for attempt in range(1, max_retries + 1):
        try:
            print("Syncing time with NTP server...")
            ntptime.settime()
            print("Time sync successful.")
            return True
        except Exception as e:
            print(f"Failed to sync time (attempt {attempt}): {e}")
            time.sleep(delay)
    print("Time sync failed after retries.")
    return False
        
def is_daytime_now():
#    t = time.localtime()
    t = localtime_with_offset()
    hour = t[3]  # Hour is the 4th element in the tuple
    return 7 <= hour < 19  # Define day as between 7am and 7pm (0700 to 1900)

def is_us_dst_now():
    """Return True if the current UTC time is in US DST period (2nd Sunday in March to 1st Sunday in November)."""
    t = time.gmtime()
    year = t[0]

    # Find second Sunday in March
    march = [time.mktime((year, 3, day, 2, 0, 0, 0, 0)) for day in range(8, 15)]
    dst_start = next(ts for ts in march if time.localtime(ts)[6] == 6)

    # Find first Sunday in November
    november = [time.mktime((year, 11, day, 2, 0, 0, 0, 0)) for day in range(1, 8)]
    dst_end = next(ts for ts in november if time.localtime(ts)[6] == 6)

    now = time.mktime(t)
    return dst_start <= now < dst_end

def apply_gmt_offset_from_settings(settings):
    global gmt_offset, gmt_offset_complete

    tz = settings.get("timezone")
    use_dst = settings.get("use_dst", False)
    manual_offset_raw = settings.get("manual_offset", "")

    if tz == "Manual":
        try:
            gmt_offset = float(manual_offset_raw)
            print(f"Using manual GMT offset: {gmt_offset} hours")
        except Exception:
            gmt_offset = 0
            print("Invalid manual offset; defaulting to GMT+0")
    else:
        base_offset = TIMEZONE_OFFSETS.get(tz)
        if base_offset is not None:
            gmt_offset = base_offset
            if use_dst and is_us_dst_now():
                gmt_offset += 1
                print(f"{tz} is in DST, adjusted GMT offset: {gmt_offset} hours")
            else:
                print(f"{tz}, standard GMT offset: {gmt_offset} hours")
        else:
            gmt_offset = 0
            print(f"Unknown timezone '{tz}'; defaulting to GMT+0")

    gmt_offset_complete = True

def localtime_with_offset():
#    Return local time.struct_time adjusted from UTC using timezone offset and DST.
    
#    now = time.gmtime()
#    month = now[1]
#    mday = now[2]
#    weekday = now[6]  # 0 = Monday, 6 = Sunday
    now = time.mktime(time.gmtime())
    offset = gmt_offset or 0
    local_timestamp = now + int(offset * 3600)
    return time.localtime(local_timestamp)

def format_12h_time(t):
    hour = t[3]
    am_pm = "AM"
    if hour == 0:
        hour_12 = 12
    elif hour > 12:
        hour_12 = hour - 12
        am_pm = "PM"
    elif hour == 12:
        hour_12 = 12
        am_pm = "PM"
    else:
        hour_12 = hour

    return "{:2d}:{:02d} {}".format(hour_12, t[4], am_pm)

def update_time_only(time_str):
    display.fill_rect(0, 40, 240, 20, color565(0, 0, 0))  # Clear just time area
    center_lgtext(time_str, 40, color565(0, 255, 255))
    
def update_date_only(date_str):
    display.fill_rect(0, 20, 240, 20, color565(0, 0, 0))  # Clear just date area
    center_lgtext(date_str, 20, color565(255, 255, 255))
    
def fetch_sunrise_sunset(lat, lon, gmt_offset_hours):
    url = f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&formatted=0"
    try:
        r = urequests.get(url)
        data = r.json()
        r.close()
        if data["status"] != "OK":
            print("Sunrise-Sunset API error:", data["status"])
            return None, None
        sunrise_utc = data["results"]["sunrise"]  # ISO 8601 UTC string
        sunset_utc = data["results"]["sunset"]

        # Convert ISO 8601 to epoch seconds (MicroPython may not have dateutil)
        sunrise_epoch = iso8601_to_epoch(sunrise_utc)
        sunset_epoch = iso8601_to_epoch(sunset_utc)

        # Apply offset for local time (including DST)
        offset_sec = int(gmt_offset_hours * 3600)
        sunrise_local = time.localtime(sunrise_epoch + offset_sec)
        sunset_local = time.localtime(sunset_epoch + offset_sec)

        return sunrise_local, sunset_local

    except Exception as e:
        print("Error fetching sunrise/sunset:", e)
        return None, None

def iso8601_to_epoch(iso_str):
    # Example iso_str: "2025-06-21T09:32:00+00:00"
    # Parse manually (MicroPython usually lacks full datetime parsing)
    # This is a minimal parser:
    try:
        date_part, time_part = iso_str.split("T")
        year, month, day = map(int, date_part.split("-"))
        time_str = time_part.split("+")[0].split("Z")[0]
        hour, minute, second = map(int, time_str.split(":"))
        # Convert to epoch seconds (approximate using time.mktime and assuming no timezone)
        return time.mktime((year, month, day, hour, minute, second, 0, 0))
    except:
        return 0
    
def format_sun_time(t):
    # t is a time.struct_time or tuple like (year, month, day, hour, minute, second, ...)
    hour = t[3]
    minute = t[4]
    am_pm = "AM" if hour < 12 else "PM"
    hour_12 = hour % 12
    if hour_12 == 0:
        hour_12 = 12
    return f"{hour_12}:{minute:02d} {am_pm}"

# === Forecast Icon selection ====
def get_icon_filename(simplified_now, day):
    
    if not simplified_now:
        simplified_now = "No Forecast"
    f = simplified_now.lower()
    print(f"simplified forecast: {f}")

    icon_filename = None

    if "partly sunny" in f or "partly clear" in f or "p sunny" in f or "p clear" in f:
        icon_filename = "icons/part_cloudy_day_rgb565.raw" if day else "icons/part_cloudy_night_rgb565.raw"
    elif "mostly sunny" in f or "m sunny" in f or "mostly clear" in f or "m clear" in f:
        icon_filename = "icons/clear_day_rgb565.raw" if day else "icons/clear_night_rgb565.raw"
    elif "partly cloudy" in f or "p cloudy" in f:
        icon_filename = "icons/part_cloudy_day_rgb565.raw" if day else "icons/part_cloudy_night_rgb565.raw"
    elif "mostly cloudy" in f or "m cloudy" in f:
        icon_filename = "icons/cloudy_rgb565.raw" if day else "icons/cloudy_rgb565.raw"
    elif "sun" in f or "clear" in f:
        # Fallback for simple "sunny" or "clear" without qualifiers
        icon_filename = "icons/clear_day_rgb565.raw" if day else "icons/clear_night_rgb565.raw"
    elif "tstorms" in f or "thunderstorm" in f or "thunderstorms" in f or "t-storm" in f:
        icon_filename = "icons/tstorm_rgb565.raw"
    elif "cloud" in f or "overcast" in f:
        icon_filename = "icons/cloudy_rgb565.raw"
    elif "rain" in f or "showers" in f or "drizzle" in f:
         icon_filename = "icons/rain_rgb565.raw"
    elif "fog" in f or "haze" in f:
        icon_filename = "icons/fog_rgb565.raw"
    elif "snow" in f or "flurries" in f or "sleet" in f or "hail" in f:
        icon_filename = "icons/snow_rgb565.raw"
    elif "wind" in f:
        icon_filename = "icons/windy_rgb565.raw"
    # If nothing matches, show clear icon (NOTE: CHANGE TO SOMETHING ELSE, smiley, world, etc)
    else:
        icon_filename = "icons/smiley_rgb565.raw" if day else "icons/smiley_sleep_rgb565.raw"
    
    print(f"Icon filename selected: {icon_filename}")
    return icon_filename

# ==== display/drawing functions ====

def replace_color_rgb565(data, from_color, to_color):
    out = bytearray(len(data))
    for i in range(0, len(data), 2):
        color = (data[i] << 8) | data[i+1]
        if color == from_color:
            color = to_color
        out[i] = color >> 8
        out[i+1] = color & 0xFF
    return out

def rgb565_to_rgb888(color):
    r = ((color >> 11) & 0x1F) << 3
    g = ((color >> 5) & 0x3F) << 2
    b = (color & 0x1F) << 3
    return r, g, b

def rgb888_to_rgb565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)

def display_raw_image_in_chunks(display, filepath, x, y, width, height, scale=1, smooth=False, chunk_rows=8, clear_color=0x0000, clear=True):
    """
    Streams a raw RGB565 image to the GC9A01 display in chunks using blit_buffer(),
    with optional integer scaling and sharpening (sharpening applied after scaling).

    Args:
        display:     Initialized GC9A01 display object.
        filepath:    Path to the .raw RGB565 image file.
        x, y:        Top-left position on the screen to draw the image.
        width:       Width of the image in pixels.
        height:      Height of the image in pixels.
        scale:       Integer scaling factor (default: 1 = no scale).
        smooth:      Add optional smoothing after scaling
        chunk_rows:  Number of source image rows per chunk (default: 8).
        clear_color: Optional background color (default: black).
        clear:       If True, clear the screen before drawing.
    """
    import gc

    def smooth_chunk(data, width, height, threshold=10):
        out = bytearray(len(data))

        for row in range(height):
            for col in range(width):
                center_idx = (row * width + col) * 2
                center = (data[center_idx] << 8) | data[center_idx + 1]
                r_sum, g_sum, b_sum, count = 0, 0, 0, 0

                for dy in (-1, 0, 1):
                    ny = row + dy
                    if ny < 0 or ny >= height:
                        continue
                    for dx in (-1, 0, 1):
                        nx = col + dx
                        if nx < 0 or nx >= width:
                            continue
                        neighbor_idx = (ny * width + nx) * 2
                        neighbor = (data[neighbor_idx] << 8) | data[neighbor_idx + 1]

                        # Use your existing helper for RGB conversion
                        r1, g1, b1 = rgb565_to_rgb888(center)
                        r2, g2, b2 = rgb565_to_rgb888(neighbor)
                        dist = abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)

                        if dist <= threshold:
                            r_sum += r2
                            g_sum += g2
                            b_sum += b2
                            count += 1

                if count > 0:
                    avg_r = r_sum // count
                    avg_g = g_sum // count
                    avg_b = b_sum // count
                    smoothed = rgb888_to_rgb565(avg_r, avg_g, avg_b)
                else:
                    smoothed = center

                out[center_idx] = smoothed >> 8
                out[center_idx + 1] = smoothed & 0xFF

        return out

    bytes_per_pixel = 2
    row_bytes = width * bytes_per_pixel

    if clear:
        display.fill(clear_color)


    try:
        with open(filepath, "rb") as f:
            for row_start in range(0, height, chunk_rows):
                actual_rows = min(chunk_rows, height - row_start)
                chunk_size = actual_rows * row_bytes
                chunk_data = f.read(chunk_size)

                if scale == 1:
                    display.blit_buffer(chunk_data, x, y + row_start, width, actual_rows)
                else:
                    scaled_width = width * scale
                    scaled_height = actual_rows * scale
                    scaled_chunk = bytearray(scaled_width * scaled_height * 2)

                    for row in range(actual_rows):
                        for col in range(width):
                            src_idx = (row * width + col) * 2
                            pixel_hi = chunk_data[src_idx]
                            pixel_lo = chunk_data[src_idx + 1]

                            for dy in range(scale):
                                dest_row = row * scale + dy
                                for dx in range(scale):
                                    dest_col = col * scale + dx
                                    dest_idx = (dest_row * scaled_width + dest_col) * 2
                                    scaled_chunk[dest_idx] = pixel_hi
                                    scaled_chunk[dest_idx + 1] = pixel_lo

                    if smooth:
                        scaled_chunk = smooth_chunk(scaled_chunk, scaled_width, scaled_height)

                    display.blit_buffer(scaled_chunk, x, y + row_start * scale, scaled_width, scaled_height)

                gc.collect()
                
    except Exception as e:
        print("Error displaying image:", e)

def display_1bit_image_in_chunks(display, path, x0, y0, width, height, fg_color, bg_color):
    row_bytes = width // 8  # bytes per row in 1-bit format
    with open(path, "rb") as f:
        for y in range(height):
            row = f.read(row_bytes)
            buf = bytearray(width * 2)  # one line of RGB565
            for x in range(width):
                byte_index = x // 8
                bit_index = 7 - (x % 8)
                bit = (row[byte_index] >> bit_index) & 1
                color = fg_color if bit else bg_color
                i = x * 2
                buf[i] = color >> 8
                buf[i + 1] = color & 0xFF
            display.blit_buffer(buf, x0, y0 + y, width, 1)
            
def draw_sparse_grayscale(display, filepath):
    with open(filepath, "rb") as f:
        while True:
            bytes_read = f.read(3)
            if not bytes_read or len(bytes_read) < 3:
                break
            x, y, gray = bytes_read
            # Convert grayscale to RGB565 (approximate)
            rgb565 = ((gray & 0xF8) << 8) | ((gray & 0xFC) << 3) | (gray >> 3)
            display.pixel(x, y, rgb565)

def draw_sparse_1bit(display, filepath, color=0x0000):
    with open(filepath, "rb") as f:
        while True:
            bytes_read = f.read(2)
            if not bytes_read or len(bytes_read) < 2:
                break
            x, y = bytes_read
            display.pixel(x, y, color)

def draw_weather_icon(gc9a01, simplified_now, x, y, is_daytime=None):
#    gc9a01.fill_rect(x, y, 48, 32, 0)
    if is_daytime is None:
        day = is_daytime_now()  # use calculated day/night indication
    else:
        day = is_daytime  # use forecast day/night indication
    icon_filename = get_icon_filename(simplified_now, day)
    if icon_filename:
        try:
            with open(icon_filename, "rb") as f:
                icon_data = f.read()
            gc9a01.blit_buffer(icon_data, x, y, 64, 64)

        except OSError:
            gc9a01.text(font_lg, "Err", x, y, color565(255, 0, 0))
    else:
        gc9a01.text(font_lg, "N/A", x, y, color565(255, 0, 0))

# Determine how many pixels acress at a given row for the round display
def row_visible_width(y, diameter=240):
    r = diameter // 2
    dy = abs(y - r)
    if dy > r:
        return 0  # outside the circle
    return int(2 * math.sqrt(r**2 - dy**2))

def center_smtext(text, y, fg=color565(255,255,255), bg=color565(0,0,0)):
    visible_width = row_visible_width(y)
    text_width = len(text) * 8   # 8 pixel wide text
    if visible_width == 0:
        return
    x = (240 - visible_width) // 2 + (visible_width - text_width) // 2
    display.text(font_sm, text, x, y, fg, bg)
    
def center_lgtext(text, y, fg=color565(255,255,255), bg=color565(0,0,0)):
    visible_width = row_visible_width(y)
    text_width = len(text) * 16   # 16 pixel wide text
    if visible_width == 0:
        return
    x = (240 - visible_width) // 2 + (visible_width - text_width) // 2
    display.text(font_lg, text, x, y, fg, bg)
    
def center_hugetext(text, y, fg=color565(255,255,255), bg=color565(0,0,0)):
    visible_width = row_visible_width(y)
    text_width = len(text) * 16   # 16 pixel wide text
    if visible_width == 0:
        return
    x = (240 - visible_width) // 2 + (visible_width - text_width) // 2
    display.text(font_huge, text, x, y, fg, bg)

# === Determine latitude and longitude from zip code ===
def get_lat_lon(zip_code, country_code="us"):
    url = f"http://api.zippopotam.us/{country_code}/{zip_code}"
    try:
        response = urequests.get(url)
        if response.status_code == 200:
            data = response.json()
            place = data["places"][0]
            lat = float(place["latitude"])
            lon = float(place["longitude"])
            return lat, lon, "Lat/Lon Lookup OK"
        elif response.status_code == 404:
            print(f"Invalid Zip Code: {zip_code}")
            return None, None, "Invalid Zip Code"
        else:
            print(f"Unexpected API status code: {response.status_code}")
            return None, None, "Lat/Lon Lookup Site Error"
    except Exception as e:
        print("WiFi or API error during Zip to lat/lon lookup:", repr(e))
    return None, None, "WiFi or Site Error"


# === Get Sunrise and sunset times if needed ====
def update_sun_times_if_needed(lat, lon, gmt_offset, dst):
    global sunrise, sunset, last_sun_fetch_day
    now = time.localtime()
    today = (now[0], now[1], now[2])  # year, month, day

    if last_sun_fetch_day != today:
        print("Fetching new sunrise/sunset times...")
        sr, ss = fetch_sunrise_sunset(lat, lon, gmt_offset, dst)
        if sr and ss:
            sunrise = sr
            sunset = ss
            last_sun_fetch_day = today
            print(f"Sunrise: {format_sun_time(sr)}, Sunset: {format_sun_time(ss)}")
        else:
            print("Failed to fetch sunrise/sunset times")
            
# === Helpers for extracting strings and data from json and streams ====

def extract_first_json_string_value(raw_json, key):
    """
    Extracts the first string value for a given key in raw JSON text.
    Returns the string value, or None if not found.
    
    This is lightweight and avoids parsing large JSON structures.
    """
    search_key = f'"{key}"'
    idx = raw_json.find(search_key)
    if idx == -1:
        return None

    # Find the colon separating key and value
    colon_idx = raw_json.find(":", idx + len(search_key))
    if colon_idx == -1:
        return None

    # Find the surrounding double quotes around the string value
    start_quote = raw_json.find('"', colon_idx + 1)
    if start_quote == -1:
        return None
    end_quote = raw_json.find('"', start_quote + 1)
    if end_quote == -1:
        return None

    return raw_json[start_quote + 1:end_quote]

def extract_first_json_string_value_stream(response_stream, key):
    """
    Stream‐parse response_stream for the first JSON string field "key":"value"
    without loading the full response into RAM.
    """
    buf = b""
    max_buf = 4096
    # Compile regex pattern like: b'"shortForecast"\s*:\s*"([^"]+)"'
    pattern = b'"' + key.encode("utf-8") + b'"\\s*:\\s*"([^"]+)"'
    regex = ure.compile(pattern)

    while True:
        chunk = response_stream.read(256)
        if not chunk:
            break
        buf += chunk
        if len(buf) > max_buf:
            buf = buf[-max_buf:]
        match = regex.search(buf)
        if match:
            return match.group(1).decode("utf-8")
    return None

def fetch_first_station_id(obs_station_url, headers):
    """
    Stream‐parse the /stations FeatureCollection for the first feature.id
    that contains '/stations/', extracting the station code at the end.
    """
    print("Fetching observation stations list…")
    r = urequests.get(obs_station_url, headers=headers)
    stream = r.raw

    buf = b""
    key = b'"id":'
    max_buf = 4096  # keep up to 4 KB in memory

    while True:
        chunk = stream.read(256)
        if not chunk:
            break
        buf += chunk
        # Trim buffer
        if len(buf) > max_buf:
            buf = buf[-max_buf:]

        # Look for `"id":` in buffer
        idx = buf.find(key)
        if idx != -1:
            # Find the opening quote for the URL
            start_quote = buf.find(b'"', idx + len(key))
            if start_quote != -1:
                end_quote = buf.find(b'"', start_quote + 1)
                if end_quote != -1:
                    url = buf[start_quote + 1:end_quote].decode("utf-8")
                    # Only accept URLs that point to a station
                    if "/stations/" in url:
                        station_id = url.rsplit("/", 1)[-1]
                        print("Extracted station_id:", station_id)
                        r.close()
                        gc.collect()
                        return station_id
                    # otherwise keep searching after this index
                    buf = buf[end_quote+1:]
    r.close()
    gc.collect()
    print("Failed to extract stationIdentifier from stream.")
    return None

def extract_first_number_stream_generic(stream, pattern):
    """
    Stream-parse `stream` to find the first numeric value matching `pattern`.
    - stream: a file-like object supporting .read()
    - pattern: a bytes regex with one capture group for the number, e.g.
        rb'"temperature"\s*:\s*([0-9]+(?:\.[0-9]+)?)'
        rb'"relativeHumidity"\s*:\s*\{[^}]*"value"\s*:\s*([0-9]+(?:\.[0-9]+)?)'
    Returns:
      float(parsed_number) on success,
      None if no match or parse error.
    """
    buf = b""                         # rolling buffer of recent bytes
    max_buf = 4096                    # cap buffer at 4 KB to limit RAM use
    prog = ure.compile(pattern)       # compile the regex once

    while True:
        chunk = stream.read(256)      # read small 256-byte chunks
        if not chunk:
            break                     # end of stream

        buf += chunk
        if len(buf) > max_buf:
            buf = buf[-max_buf:]      # drop oldest data beyond 4 KB

        m = prog.search(buf)          # search buffer for the pattern
        if m:
            # m.group(1) is the first capture—our numeric string
            try:
                return float(m.group(1))
            except Exception:
                return None

    return None                       # no match found

def titlecase(s):
    return ' '.join(word.capitalize() for word in s.split())

# === Weather related functions ===

def get_nws_metadata(lat, lon):
    try:
        headers = {"User-Agent": USER_AGENT}

        # Step 1: Get point data for the lat/lon
        print("Fetching point data:", f"https://api.weather.gov/points/{lat},{lon}")
        r = urequests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers)
        
        if r.status_code == 404:
            try:
                err_data = r.json()
                detail = err_data.get("detail", "").lower()
                title = err_data.get("title", "").lower()
                error_type = err_data.get("type", "").lower()

                if (
                    "outside the forecast area" in detail
                    or "unable to provide data" in detail
                    or "invalid point" in title
                    or "invalidpoint" in error_type
                ):
                    return "Location outside US NWS area"
                else:
                    return f"NWS error: {detail}"
            except Exception:
                return "NWS 404 error"

        if r.status_code != 200:
            return f"NWS status {r.status_code}"
        
        raw = r.text
        print("Downloaded length (point data):", len(raw))
        r.close()

        point_data = json.loads(raw)
        properties = point_data.get("properties", {})

        forecast_url = properties.get("forecast")
        obs_station_url = properties.get("observationStations")
        forecast_hourly_url = properties.get("forecastHourly")

        office = properties.get("gridId")
        grid_x = properties.get("gridX")
        grid_y = properties.get("gridY")

        # Construct fallback hourly forecast URL if missing
        if not forecast_hourly_url and office and grid_x is not None and grid_y is not None:
            forecast_hourly_url = f"https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}/forecast/hourly"

        # ✅ Check for None before fetching observation stations
        if not obs_station_url:
            print("No observationStations URL found.")
            return "NWS metadata error: no station URL"

        # Fetch the first observation station ID
        print("Fetching observation stations list:", obs_station_url)
        station_id = fetch_first_station_id(obs_station_url, headers)

        # Clean up to free memory
        del raw, point_data, properties
        gc.collect()

        if not station_id:
            print("No observation station found.")
            return None

        return {
            "forecast_url": forecast_url,
            "hourly_url": forecast_hourly_url,
            "station_id": station_id
        }

    except Exception as e:
        print("Error fetching NWS metadata:", e)
        sys.print_exception(e)
        return None

import time

def parse_iso8601(s):
    """
    Parse ISO8601 string like '2025-06-19T12:56:00+00:00'
    Returns (year, month, day, hour, minute, second)
    """
    try:
        date_str, time_str = s.split('T')
        year, month, day = map(int, date_str.split('-'))
        time_part = time_str.split('+')[0].split('-')[0]  # Remove timezone offset
        hour, minute, second = map(int, time_part.split(':'))
        return (year, month, day, hour, minute, second)
    except Exception as e:
        print("Error parsing ISO8601:", e)
        return None

def to_epoch_seconds(t):
    """
    Convert tuple (year, month, day, hour, minute, second) to epoch seconds
    """
    if t is None:
        return None
    # time.mktime expects struct_time tuple with at least 8 elements
    tm = (t[0], t[1], t[2], t[3], t[4], t[5], 0, 0, 0)
    return time.mktime(tm)

def find_period_bounds(raw, pos):
    """
    Given position of '"number":' in raw JSON,
    find the start and end indices of the full JSON object
    enclosing that number field by matching braces.
    """
    # Find the start brace '{' just before pos
    start = raw.rfind('{', 0, pos)
    if start == -1:
        start = pos  # fallback

    # Now scan forward to find matching closing '}'
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == '{':
            depth += 1
        elif raw[i] == '}':
            depth -= 1
            if depth == 0:
                return start, i
    # fallback if no match found
    return start, len(raw) - 1

def extract_forecast_periods_stream(response_stream, max_night_periods=3, max_day_periods=7, max_buf=4096):
    buf = b""
    periods = []
    idx = 0
    day_count = 0
    night_count = 0
    in_periods = False # only start extracting after "periods"

    def find_balanced_braces_stream(text, start_idx):
        brace_count = 0
        started = False
        for i in range(start_idx, len(text)):
            c = text[i]
            if c == ord('{'):
                brace_count += 1
                started = True
            elif c == ord('}'):
                brace_count -= 1
                if brace_count == 0 and started:
                    return i
        return -1

    # Precompile regexes for fields
    pattern_name = ure.compile(rb'"name"\s*:\s*"([^"]*)"')
    pattern_shortForecast = ure.compile(rb'"shortForecast"\s*:\s*"([^"]*)"')
    pattern_temperature = ure.compile(rb'"temperature"\s*:\s*(\d+)')
    pattern_isDaytime = ure.compile(rb'"isDaytime"\s*:\s*(true|false)')

    def extract_str(pattern, text):
        m = pattern.search(text)
        return m.group(1).decode("utf-8") if m else ""

    def extract_int(pattern, text):
        m = pattern.search(text)
        try:
            return int(m.group(1)) if m else None
        except:
            return None

    def extract_bool(pattern, text):
        m = pattern.search(text)
        return m.group(1) == b"true" if m else False

    while True:
        chunk = response_stream.read(256)
        if not chunk:
            break
        buf += chunk
        if len(buf) > max_buf:
            # Keep only the most recent data, but preserve unprocessed tail
            unprocessed = buf[idx:]
            buf = unprocessed[-max_buf:]
            idx = 0  # reset index because buffer changed

        while True:
            if not in_periods:
                periods_start = buf.find(b'"periods": [',idx)
                if periods_start == -1:
                    break # wait for more data
                idx = periods_start + len(b'"periods": [')
                in_periods = True
                
            num_idx = buf.find(b'"number":', idx)
            if num_idx == -1:
                break  # No more periods found in buffer yet

            # Find the '{' before num_idx
            start_obj = buf.rfind(b'{', 0, num_idx)
            if start_obj == -1 or start_obj < idx:
                # fallback: find next '{' after num_idx
                start_obj = buf.find(b'{', num_idx)
                if start_obj == -1:
                    break  # can't find object start, wait for more data

            end_obj = find_balanced_braces_stream(buf, start_obj)
            if end_obj == -1:
                # incomplete JSON object; wait for more data
                break

            period_text = buf[start_obj:end_obj+1]

            # Extract fields
            name = extract_str(pattern_name, period_text)
            shortForecast = extract_str(pattern_shortForecast, period_text)
            temperature = extract_int(pattern_temperature, period_text)
            isDaytime = extract_bool(pattern_isDaytime, period_text)

            should_append = False
            if isDaytime and day_count < max_day_periods:
                day_count += 1
                should_append = True
            elif not isDaytime and night_count < max_night_periods:
                night_count += 1
                should_append = True

            if should_append:
                periods.append({
                    "name": name,
                    "shortForecast": shortForecast,
                    "temperature": temperature,
                    "isDaytime": isDaytime,
                })
                
            if day_count >= max_day_periods and night_count >= max_night_periods:
                return periods

            idx = end_obj + 1

        # Reset index if we have parsed beyond buffer end
        if idx >= len(buf):
            idx = 0

    return periods

def split_forecast_text(text):
    """Split a forecast string into two parts if it contains ' then '."""
    if not text:
        return "", None
    parts = text.split(" then ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return text.strip(), None

#def extract_forecast_periods(raw_forecast):
#    """
#    Stream-parse raw_forecast JSON text and extract forecast periods.
#    Returns a list of dicts with keys: name, shortForecast, temperature, isDaytime.
#    """
    
#    # Precompile regex patterns (memory safe)
#    pattern_name = ure.compile(r'"name"\s*:\s*"([^"]*)"')
#    pattern_shortForecast = ure.compile(r'"shortForecast"\s*:\s*"([^"]*)"')
#    pattern_temperature = ure.compile(r'"temperature"\s*:\s*(\d+)')
#    pattern_isDaytime = ure.compile(r'"isDaytime"\s*:\s*(true|false)')

#    def find_balanced_braces(text, start_idx):
#        brace_count = 0
#        started = False
#        for i in range(start_idx, len(text)):
#            c = text[i]
#            if c == '{':
#                brace_count += 1
#                started = True
#            elif c == '}':
#                brace_count -= 1
#                if brace_count == 0 and started:
#                    return i
#        return -1

#    def extract_str(pattern, text):
#        m = pattern.search(text)
#        if m:
#            return m.group(1)
#        return ""

#    def extract_int(pattern, text):
#        m = pattern.search(text)
#        if m:
#            try:
#                return int(m.group(1))
#            except:
#                return None
#        return None

#    def extract_bool(pattern, text):
#        m = pattern.search(text)
#        if m:
#            return m.group(1) == "true"
#        return False

#    periods = []
#    max_day_night_periods = 4
#    max_daytime_only_periods = 7

#    idx = 0
#    length = len(raw_forecast)
#    daynight_collected = 0
#    daytime_only_collected = 0

#    while idx < length and (daynight_collected + daytime_only_collected) < (max_day_night_periods + max_daytime_only_periods):
#        num_idx = raw_forecast.find('"number":', idx)
#        if num_idx == -1:
#            break

#        # Find start of object by locating '{' before or at num_idx
#        start_obj = raw_forecast.rfind('{', 0, num_idx)
#        if start_obj == -1 or start_obj < idx:
#            # fallback to find next '{' after num_idx
#            start_obj = raw_forecast.find('{', num_idx)
#            if start_obj == -1:
#                break

#        end_obj = find_balanced_braces(raw_forecast, start_obj)
#        if end_obj == -1:
#            break

#        period_text = raw_forecast[start_obj:end_obj+1]

#        name = extract_str(pattern_name, period_text)
#        shortForecast = extract_str(pattern_shortForecast, period_text)
#        temperature = extract_int(pattern_temperature, period_text)
#        isDaytime = extract_bool(pattern_isDaytime, period_text)

#        if daynight_collected < max_day_night_periods:
#            periods.append({
#                "name": name,
#                "shortForecast": shortForecast,
#                "temperature": temperature,
#                "isDaytime": isDaytime,
#            })
#            daynight_collected += 1
#        else:
#            if isDaytime and daytime_only_collected < max_daytime_only_periods:
#                periods.append({
#                    "name": name,
#                    "shortForecast": shortForecast,
#                    "temperature": temperature,
#                    "isDaytime": isDaytime,
#                })
#                daytime_only_collected += 1

#        idx = end_obj + 1
#        gc.collect()

#    return periods

def get_weather_data(lat, lon, metadata, headers):
    try:

        # Unload fonts to free up memory
#        unload_all_fonts()
        periods = []
        # Validate cached metadata
        station_id = metadata.get("station_id") if metadata else None
        forecast_url = metadata.get("forecast_url") if metadata else None
        hourly_url = metadata.get("hourly_url") if metadata else None
        # Retry fetching metadata if missing
        if not station_id or not forecast_url or not hourly_url:
            print("Metadata incomplete, refreshing metadata...")
            metadata = get_nws_metadata(lat, lon)
            
            if isinstance(metadata, str):
                print("Metadata fetch error (non-fatal):", metadata)
                return [{
                    "name": "N/A",
                    "shortForecast": "N/A",
                    "simpleForecast": "N/A",
                    "temperature": None,
                    "isDaytime": None,
                }]
            
            if not metadata:
                print("Failed to refresh metadata, returning None")
                return [{
                    "name": "N/A",
                    "shortForecast": "N/A",
                    "simpleForecast": "N/A",
                    "temperature": None,
                    "isDaytime": None,
                }]
            
            station_id = metadata.get("station_id")
            forecast_url = metadata.get("forecast_url")
            hourly_url = metadata.get("hourly_url")

        # Do forecast fetch for multi=day forecast
        print("Fetching URL:", forecast_url)
        print("Before fetching forecast JSON:")
        print_memory_usage()
        test_free_memory()
        
        period = []
        try:
            r = urequests.get(forecast_url, headers=headers)
            gc.collect()
            
            periods = extract_forecast_periods_stream(r.raw)
            gc.collect()
            r.close()
            del r
            gc.collect()
            
            # DEBUG: print what was parsed
            print("Parsed forecast periods:")
            for i, f in enumerate(periods):
                print(f"Period {i}: name={f['name']!r}, shortForecast={f['shortForecast']!r}")

            print("After fetching forecast JSON (raw text in memory):")
            print_memory_usage()
            test_free_memory()

            # Extract multiple forecast periods
            # Post-process each period to simplify forecast and trim name
            for period in periods:
                short_forecast = period.get("shortForecast", "")
                forecast1, forecast2 = split_forecast_text(short_forecast)

                period["forecast1"] = forecast1
                period["forecast2"] = forecast2 
                period["simpleForecast"] = simplify_forecast(short_forecast)
                period["forecast1_short"] = simplify_forecast(forecast1)
                period["forecast2_short"] = simplify_forecast(forecast2) if forecast2 else None
                
            if not periods:
                periods = [{
                    "name": "N/A",
                    "shortForecast": "N/A",
                    "simpleForecast": "N/A",
                    "temperature": None,
                    "isDaytime": None,
                }]
            
            print(f"Extracted {len(periods)} forecast periods")
            for i, period in enumerate(periods):
                print(f"Period {i}: name='{period.get('name', '')}'")
                print(f"Period {i}: shortForecast='{period.get('shortForecast', '')}'")
                print(f"Period {i}: simpleForecast='{period.get('simpleForecast', '')}'")
                print(f"Period {i}: forecast1='{period['forecast1']}'")
                print(f"Period {i}: forecast1_short='{period['forecast1_short']}'")
                if period['forecast2']:
                    print(f"          forecast2='{period['forecast2']}'")
                    print(f"          forecast2_short='{period['forecast2_short']}'")
            print("After extracting forecast periods")
            print_memory_usage()

            print("After freeing raw forecast JSON and running GC:")
            print_memory_usage()
            
        except Exception as e:
            print("Error fetching or parsing forecast data:", e)
            periods = [{
                "name": "N/A",
                "shortForecast": "N/A",
                "forecast1": "N/A",
                "forecast2": None,
                "simpleForecast": "N/A",
                "temperature": None,
                "isDaytime": None,
            }]
                  
        # Return the final values
        return periods

    except Exception as e:
        print("Error in get_weather_data:", e)
        sys.print_exception(e)
        
        return [{
            "name": "N/A",
            "shortForecast": "N/A",
            "forecast1": "N/A",
            "forecast2": None,
            "simpleForecast": "N/A",
            "temperature": None,
            "isDaytime": None,
        }]

def shorten_period_name(name):
    """Shorten forecast period names to fit within 14 characters."""
    if not name:
        return ""

    name = name.strip()

    # Mapping for known long holidays
    holiday_map = {
        "Thanksgiving Day": "Thanksgiving",
        "Christmas Day": "Christmas",
        "Christmas Night": "Xmas Night",
        "New Year's Day": "New Year",
        "New Year's Night": "New Year Night",
        "Independence Day": "July 4",
        "Washington's Birthday": "Presidents",
        "Martin Luther King Jr. Day": "MLK Day",
    }

    if name in holiday_map:
        return holiday_map[name]

    # Handle "<Day of Week> Night" → "Mon Night"
    if name.endswith("Night"):
        parts = name.split()
        if len(parts) == 2 and parts[1] == "Night":
            day = parts[0]
            return day[:3] + " Night"

    # Just shorten long day names if needed
    if len(name) > 14:
        return name[:14]  # truncate if absolutely necessary

    return name

def simplify_forecast(forecast):
    MODIFIERS = ["Slight Chance", "Light", "Chance", "Mostly", "Partly", "Partial",
                 "Shallow", "Patches", "Patchy", "Likely", "Heavy", "Scattered",
                 "Isolated", "Drifting", "Blowing", "Few", "Broken", "Widespread",
                 "Frequent", "Gust", "Gusty", "Intermittent", "Increasing", "Occasional",
                 "Variable"
    ]
    CONDITIONS = [
        "Tornado", "Funnel Cloud", "Hailstorm", "Hailstorms", "Blizzard", "Winter Storm", "Winter Weather"
        "Freezing Rain", "Freezing Drizzle", "Hail", "Sleet", "Ice", "Frost",
        "Flash Flood", "Flood", "Dust Storm", "Smoke", "Volcanic Ash", "Dust", "Spray", "Sand",
        "Hurricane", "Tropical storm", "Thunderstorms", "Sandstorm",
        "Thunderstorm", "T-storms", "Tstorms", "Lightning"
        "Storm", "Squall", "Showers", "Rain", "Precipitation"
        "Fog", "Snow", "Clear", "Sunny",
        "Cloudy", "Overcast", "Windy", "Gusty", "Wind", "Drizzle",
        "Haze", "Mist", "Snow Grains", "Ice Crystals", "Ice Pellets", "Snow Pellets"
    ]
    # First, make sure there is a valid forecast
    if not forecast or not isinstance(forecast, str):
        return "No Forecast"
    
    # Define priority by order in CONDITIONS list (lower index = higher priority)
    # Find highest priority condition (lowest index in CONDITIONS)

    # Cut off forecast at any strong separator (only use "current" condition)
    for sep in [" then ", ";", ","]:
        if sep in forecast.lower():
            forecast = forecast.lower().split(sep, 1)[0]
            break

    forecast = forecast.strip().lower()

    found_modifiers = []
    found_conditions = []

    # Find all modifiers present with positions
    for mod in MODIFIERS:
        pos = forecast.find(mod.lower())
        if pos != -1:
            found_modifiers.append((pos, mod))

    # Find all conditions present with positions
    for cond in CONDITIONS:
        pos = forecast.find(cond.lower())
        if pos != -1:
            found_conditions.append((pos, cond))

    # Pick earliest modifier if any
    found_modifiers.sort(key=lambda x: x[0])
    found_modifier = found_modifiers[0][1] if found_modifiers else ""

    # Pick highest priority condition present:
    # conditions with lowest index in CONDITIONS list are highest priority
    priority_found_conditions = [(CONDITIONS.index(cond), pos, cond)
                                 for pos, cond in found_conditions if cond in CONDITIONS]
    priority_found_conditions.sort()  # sorts by priority index, then position, then cond
    found_condition = priority_found_conditions[0][2] if priority_found_conditions else ""

    # Special rules for modifiers + conditions to keep total under 14 characters
    # First, if no modifier, just check for the over 14 character conditions and shorten
    if not found_modifier:
        if found_condition.lower() =="freezing drizzle":
            found_condition = "Frzing Drizzle"
 
    # If get here, there is modifier, to check modifiers and conditions
    else:    
        #First check modifiers and make 6 chars or less
        if found_modifier.lower() == "isolated":
            found_modifier = "Isol"
        if found_modifier.lower() == "slight chance":
            found_modifier = "Chance"
        if found_modifier.lower() == "scattered":
            found_modifier = "Scattr"
        if found_modifier.lower() == "partial":
            found_modifier = "Prtial"
        if found_modifier.lower() == "shallow":
            found_modifier = "Shllow"
        if found_modifier.lower() == "patches":
            found_modifier = "Patchy"
        if found_modifier.lower() == "drifting":
            found_modifier = "Drftng"
        if found_modifier.lower() == "blowing":
            found_modifier = "Blowng"
        if found_modifier.lower() == "widespread":
            found_modifier = "Wdsprd"
        if found_modifier.lower() == "frequent":
            found_modifier = "Frqunt"
        if found_modifier.lower() == "intermittent":
            found_modifier = "Intmit"
        if found_modifier.lower() == "increasing":
            found_modifier = "Increa"
        if found_modifier.lower() == "occasional":
            found_modifier = "Occasl"
        if found_modifier.lower() == "variable":
            found_modifier = "Variab"
        # Next check conditions and make 7 chars or less
        if found_condition.lower() =="hailstorm":
            found_condition = "Hailstrm"
        if found_condition.lower() =="hailstorms":
            found_condition = "Hailstrm"
        if found_condition.lower() =="blizzard":
            found_condition = "Blizzrd"
        if found_condition.lower() =="winter storm":
            found_condition = "Win Stm"
        if found_condition.lower() =="winter weather":
            found_condition = "Win Weth"
        if found_condition.lower() =="freezing rain":
            found_condition = "Fr Rain"
        if found_condition.lower() =="freezing drizzle":
            found_condition = "Fr Drzl"
        if found_condition.lower() =="flash flood":
            found_condition = "Fl Flood"
        if found_condition.lower() =="dust storm":
            found_condition = "Dust St"
        if found_condition.lower() =="volcanic ash":
            found_condition = "Volc Ash"
        if found_condition.lower() =="hurricane":
            found_condition = "Hurrcan"
        if found_condition.lower() =="tropical storm":
            found_condition = "Trop St"
        if found_condition.lower() =="thunderstorm":
            found_condition = "Tstorms"
        if found_condition.lower() =="thunderstorms":
            found_condition = "Tstorms"
        if found_condition.lower() =="thunderstorms":
            found_condition = "Tstorms"
        if found_condition.lower() =="t-storms":
            found_condition = "Tstorms"
        if found_condition.lower() =="precipitation":
            found_condition = "Precip"
        if found_condition.lower() =="funnel cloud":
            found_condition = "FunlCld"
        if found_condition.lower() =="sandstorm":
            found_condition = "SndStrm"
        if found_condition.lower() =="snow grains":
            found_condition = "Snw Grs"
        if found_condition.lower() =="ice crystals":
            found_condition = "Ice Xtl"
        if found_condition.lower() =="ice pellets":
            found_condition = "Ice Plt"
        if found_condition.lower() =="snow pellets":
            found_condition = "Snw Plt"
        if found_condition.lower() =="overcast":
            found_condition = "Ovrcast"
        if found_condition.lower() =="lightning":
            found_condition = "Lightng"
            
    phrase = f"{found_modifier} {found_condition}".strip()

    if not found_condition and not found_modifier:
        # Fallback: just use first 14 chars of forecast, capitalized
        print("No Condition or Modifier found - Phrase:", phrase, "| type:", type(phrase))
        print("Using truncated Forecast - Forecast:", forecast, "| type:", type(forecast))
        s = forecast[:14]
        return s[0].upper() + s[1:] if s else s
    
    # Return capitalized short forecast, <modifier> <condition>, truncated to 14 chars
    print("phrase:", phrase, "| type:", type(phrase))
    return phrase[:14]

    
def display_weather(interval, temp, humidity, description, is_daytime=None):
    # Clear only the areas we'll update (not the whole screen)
#     display.fill_rect(0, 0, 240, 60, color565(0, 0, 0))     # header
    display.fill_rect(0, 60, 240, 180, color565(0, 0, 0))   # lower part
    

    center_lgtext(f"{interval}", 125, color565(220, 170, 240))
    line = description
    icon_x = (240 - 63) // 2  # Centered icon
    draw_weather_icon(display, line, icon_x, 60, is_daytime)
    
    # Display 14 character weather conditions
    center_hugetext(line, 140, color565(255, 255, 0))

    if humidity is not None:
        display.text(font_huge, f"{temp}F", 50, 175, color565(255, 100, 100))
        display.text(font_huge, f"{int(humidity)}%", 130, 175, color565(100, 255, 100))
    else:
        try:
            prefix = "High: " if is_daytime else "Low: "
            t_val = int(temp)
            temp_str = f"{prefix}{t_val}F"
        except:
            temp_str = f"{temp}F" # fallback
        center_hugetext(temp_str, 175, color565(255, 100, 100))
        
def display_then():
    # Blank just the icon area and condition text
#    display.fill_rect(0, 60, 240, 64, color565(0, 0, 0))    # icon area
    display.fill_rect(0, 140, 240, 32, color565(0, 0, 0))   # forecast text area

#    center_hugetext("Then", 140, color565(150, 200, 255))   # soft blue/cyan
    center_lgtext("Then", 148, color565(150, 200, 255))   # soft blue/cyan
    
def display_forecast2(interval, temp, humidity, description, is_daytime=None):
    # Same layout as display_weather, but no need to clear entire lower section
    # Only clear icon and description area
    display.fill_rect(0, 60, 240, 64, color565(0, 0, 0))    # icon area
    display.fill_rect(0, 140, 240, 32, color565(0, 0, 0))   # forecast text area

    icon_x = (240 - 63) // 2  # Centered icon
    draw_weather_icon(display, description, icon_x, 60, is_daytime)

    # Forecast 2 text
    center_hugetext(description, 140, color565(255, 255, 0))  # same as forecast1
        
def format_sun_time(t):
    # t is a time.struct_time or tuple like (year, month, day, hour, minute, second, ...)
    hour = t[3]
    minute = t[4]
    second = t[5]
    # Round up if seconds >= 30
    if second >= 30:
        minute += 1
        if minute >= 60:
            minute = 0
            hour += 1
            if hour >= 24:
                hour = 0            
    am_pm = "AM" if hour < 12 else "PM"
    hour_12 = hour % 12
    if hour_12 == 0:
        hour_12 = 12
    return f"{hour_12}:{minute:02d} {am_pm}"

def display_sun_times(sunrise, sunset):
    display.fill_rect(0, 60, 240, 180, color565(0, 0, 0))  # Clear lower part
    
    if sunrise and sunset:
        sunrise_str = format_sun_time(sunrise)
        sunset_str = format_sun_time(sunset)
        
        center_lgtext("Sunrise:", 80, color565(255, 255, 0))
        center_hugetext(sunrise_str, 100, color565(255, 255, 0))
        center_lgtext("Sunset:", 140, color565(255, 160, 0))
        center_hugetext(sunset_str, 160, color565(255, 160, 0))
        
# === Weather Program ===
def application_mode(settings):
    
    print("Free memory entering application mode")
    test_free_memory()
    
    global start_update_requested
    global gmt_offset
    global sunrise, sunset, last_sun_fetch_day, last_displayed_time, last_displayed_date, last_sun_update_date
    global retry_allowed

    lat = settings["lat"]
    lon = settings["lon"]
    
    # Initial time sync
    sync_time()
    apply_gmt_offset_from_settings(settings)
    
    last_sync = time.time()
    last_weather_update = time.time()
    temp = humidity = None
    forecasts = []
    last_displayed_time = ""
    last_displayed_date = ""
    
    # Forecast update parameters
    forecast_phase = 0
    phase_start_time = 0
    forecast_interval = ""
    forecast_temp = 0
    forecast1 = None
    forecast2 = None
    forecast_day = None
    last_forecast_switch = 0
    cycle_index = 0
    cycle_length = 0
    
    # Determine Latitude and Longitude
#    lat, lon = get_lat_lon(zip_code)
    lat_lon_complete = lat is not None and lon is not None

    if not lat_lon_complete and retry_allowed:
        print("Lat/lon not available, attempting Zip lookup...")
        lat, lon, reason = get_lat_lon(settings.get("zip", "").strip())
        lat_lon_complete = lat is not None and lon is not None
        if lat_lon_complete:
            print(f"Zip to lat/lon lookup OK: {lat}, {lon}")
            settings["lat"] = lat
            settings["lon"] = lon
            save_settings(settings)
            # Update local variables with new values
            lat = settings["lat"]
            lon = settings["lon"]
        else:
            print(f"Failed to recover lat/lon. Reason: {reason}")
            if reason == "invalid_zip":
                print("Invalid ZIP detected — going to setup mode")
                retry_allowed = False
                display.fill(0)
                center_lgtext("Location Error", 80)
                center_smtext(reason, 100)
                center_smtext("Going to Setup Mode", 120)
                for count in range(5,0, -1):
                    display.fill_rect(0, 140, 240, 16, color565(0, 0, 0))
                    center_smtext(f"in {count} seconds", 160)
                    time.sleep(1)
                            
                setup_mode()
                server.run()
            else:
                print("Temporary lat/lon lookup issue — retry on next loop.")
            
    print("Latitude:", lat)
    print("Longitude:", lon)
    
    # Cache the metadata URLs and station ID once here if lat/lon are valid
    if lat_lon_complete:
        print("Fetching and caching new metadata URLs and station ID...")
        metadata = get_nws_metadata(lat, lon)
        
        if isinstance(metadata, str):
            print("Metadata error:", metadata)
            if metadata == "Location outside US NWS area":
                display.fill(0)
                center_lgtext("Location Error", 80)
                center_smtext(metadata, 100)
                center_smtext("Going to Setup Mode", 140)
                for count in range(5, 0, -1):
                    display.fill_rect(0, 160, 240, 16, color565(0, 0, 0))
                    center_smtext(f"in {count} seconds", 160)
                    time.sleep(1)

                setup_mode()
                server.run()
            else:
                print("Non-fatal metadata error — will proceed with fallback display.")
                metadata = None  # So downstream logic shows 'weather unavailable'
        
        if metadata:
            # Save metadata in global variables or a suitable global cache dict
            global cached_forecast_url, cached_hourly_url, cached_station_id
            cached_forecast_url = metadata["forecast_url"]
            cached_hourly_url = metadata["hourly_url"]
            cached_station_id = metadata["station_id"]
        else:
            print("Warning: Failed to fetch metadata. Will attempt fetch in get_weather_data.")

    # Initial weather fetch
    if lat_lon_complete:
        headers = {"User-Agent": USER_AGENT}
        new_forecasts = get_weather_data(lat, lon, metadata, headers)
        if new_forecasts:
            forecasts = new_forecasts
            
            # Fetch initial sunrise/sunset (we already have gmt_offset and dst from settings)
            sunrise, sunset = fetch_sunrise_sunset(lat, lon, gmt_offset)

            cycle_length = len(forecasts) + 1
            
            print("Sunrise: ", format_sun_time(sunrise))
            print("Sunset: ", format_sun_time(sunset))
            display_sun_times(sunrise, sunset)

        else:
            forecasts = []
            cycle_length = 1
            display.fill(color565(0, 0, 0))
            center_lgtext("Weather data", 80)
            center_lgtext("unavailable", 100)
    else:
        forecasts = []
        cycle_length = 1
        display.fill(color565(0, 0, 0))
        center_lgtext("Location data", 80)
        center_lgtext("unavailable", 100)
        
    cycle_index = 1  # Start  with forecast, sunrise/sunset already displayed
    last_forecast_switch = time.time()  # ensures your display cycle works on the correct timing

    while True:
        if start_update_requested:
            start_update_requested = False
            print("going to start update mode")
            start_update_mode()
            return   # exit application mode, switching to update mode

        # Time and weather loop - update weather every 5 mins, time every sec

        current_time = time.time()
    
        # Sync time every SYNC_INTERVAL (1 hour/3600 sec)
        if current_time - last_sync >= SYNC_INTERVAL:
            sync_time()
            last_sync = current_time
    
        # Refresh forecasts WEATH_INTERVAL (30 min/1800 sec) 
        if current_time - last_weather_update >= WEATH_INTERVAL:
            if not lat_lon_complete and retry_allowed:
                print("Lat/lon not available, attempting Zip lookup...")
                lat, lon, reason = get_lat_lon(settings.get("zip", "").strip())
                lat_lon_complete = lat is not None and lon is not None
                if lat_lon_complete:
                    print(f"Recovered lat/lon: {lat}, {lon}")
                    settings["lat"] = lat
                    settings["lon"] = lon
                    save_settings(settings)
                    # Update local variables with new values
                    lat = settings["lat"]
                    lon = settings["lon"]
                else:
                    print(f"Failed to recover lat/lon. Reason: {reason}")
                    if reason == "invalid_zip":
                        print("Invalid ZIP detected — going to setup mode")
                        retry_allowed = False
                        display.fill(0)
                        center_lgtext("Location Error", 80)
                        center_smtext(reason, 100)
                        center_smtext("Going to Setup Mode", 120)
                        for count in range(5,0, -1):
                            display.fill_rect(0, 140, 240, 16, color565(0, 0, 0))
                            center_smtext(f"in {count} seconds", 160)
                            time.sleep(1)
                            
                        setup_mode()
                        server.run()
                    # Possibly show fallback display here
                    else:
                        print("Temporary lat/lon lookup issue — retry on next loop.")
                
            if lat_lon_complete:     
                new_forecasts = get_weather_data(lat, lon, metadata, headers)
                if new_forecasts:
                    forecasts = new_forecasts
                    cycle_length = len(forecasts) + 1
                else:
                    forecasts = []
                    cycle_length = 1
                    display.fill_rect(0, 60, 240, 180, color565(0, 0, 0)) # x, y, w, h
                    center_lgtext("Weather Data", 80)
                    center_lgtext("Unavailable", 100)
            
            last_weather_update = current_time
        
        # Start new forecast display cycle every 10s
        if forecast_phase == 0 and time.time() - last_forecast_switch >= 10:
            print(f"Cycle index: {cycle_index}, Cycle length: {cycle_length}")
            last_forecast_switch = time.time()  # Mark 10 sec forecast cycle start
            phase_start_time = last_forecast_switch  # Mark start of inter-forecast interval phase
            
            if cycle_index == 0:
                display_sun_times(sunrise, sunset)
                forecast_phase = -1  # no follow-up phases
            elif forecasts and (cycle_index - 1) < len(forecasts):
                forecast = forecasts[cycle_index - 1]
                forecast_interval = shorten_period_name(forecast.get("name", "Forecast"))
                forecast_temp = forecast.get("temperature") or 0
                forecast1 = forecast.get("forecast1_short") or "N/A"
                forecast2 = forecast.get("forecast2_short")
                forecast_day = forecast.get("isDaytime", None)

                print(f"Interval: {forecast_interval}")
                print(f"Forecast1: {forecast1}")
                if forecast2:
                    print(f"Forecast2: {forecast2}")

                display_weather(forecast_interval, forecast_temp, None, forecast1, is_daytime=forecast_day)
                forecast_phase = 1 if forecast2 else 3  # skip intermediate phases if no forecast2
            else:
                display_weather("N/A", None, None, "N/A")
                forecast_phase = -1

        # Phase 1: After 4s, show "Then"
        elif forecast_phase == 1 and time.time() - phase_start_time >= 4:
            display_then()
            phase_start_time = time.time()
            forecast_phase = 2

        # Phase 2: After 2s, show forecast2
        elif forecast_phase == 2 and time.time() - phase_start_time >= 2:
            display_forecast2(forecast_interval, forecast_temp, None, forecast2, is_daytime=forecast_day)
            phase_start_time = time.time()
            forecast_phase = 3

        # Phase 3: Wait for remainder of 10s, then advance cycle
        elif forecast_phase == 3 and time.time() - phase_start_time >= 4:
            forecast_phase = 0
            cycle_index += 1
            if cycle_index >= cycle_length:
                cycle_index = 0

        # Phase -1: used for sunrise/sunset or N/A display, just wait 10s then reset
        elif forecast_phase == -1 and time.time() - last_forecast_switch >= 10:
            forecast_phase = 0
#            last_forecast_switch = time.time()
            cycle_index += 1
            if cycle_index >= cycle_length:
                cycle_index = 0
                
        # Get localtime *once* per loop
        now = localtime_with_offset()
        current_time_str = format_12h_time(now)
        current_date_str = "{} {}".format(MONTHS[now[1]-1], now[2])
        
        # Update time display every second
        if current_time_str != last_displayed_time:
            update_time_only(current_time_str)
            last_displayed_time = current_time_str
            
        # Update date only when it changes
        if current_date_str != last_displayed_date:
            update_date_only(current_date_str)
            last_displayed_date = current_date_str

        # Inline sunrise/sunset update logic: fetch once per local day, shortly after midnight (e.g. between 00:01 and 00:10)
        if last_sun_update_date != current_date_str:
        # Extract hour and minute from now (assuming now = (year, month, day, hour, minute, sec, wday, yday))
            hour = now[3]
            minute = now[4]

            if hour == 0 and 1 <= minute <= 10:
                # Time to fetch sunrise/sunset for the new day
                print("Fetching new sunrise/sunset data for date:", current_date_str)
                sunrise, sunset = fetch_sunrise_sunset(lat, lon, gmt_offset)
                last_sun_update_date = current_date_str

        time.sleep(0.1)  # Short sleep to maintain responsiveness

#        time.sleep(1)
    
# === Main Program - Connnect to Wifi or goto AP mode Wifi setup ===
# ===                If Wifi connection OK, go to Weather program ===
# Figure out which mode to start up in...
try:
    print("=== Free memory at start of main code ===")
    test_free_memory()
    
    # See if setup wifi switch is pressed
    if setup_sw.value() == False:
        t = 50  # Switch must be pressed for 5 seconds to reset wifi config
        while setup_sw.value() == False and t > 0:
            t -= 1
            time.sleep(0.1)
        if setup_sw.value() == False:
            print("Setup switch - entering setup mode")
            setup_mode()
            server.run()
            
    # See if settings.txt is there and valid
    status, settings, reason = load_settings()
    if status in ("missing", "invalid", "corrupt"):
        # Display reason for error in settings
        display.fill(color565(0, 0, 0))
        center_lgtext("Settings Error", 60)
        center_smtext(reason, 80)
        center_smtext("Entering Setup Mode", 120)
        for count in range(5,0, -1):   # Count down from 5 to 1
            display.fill_rect(0, 140, 240, 16, color565(0, 0, 0))  # Clears 1 text line
            center_smtext(f"in {count} seconds", 140)
            time.sleep(1)
        print(f"Settings status = {status}. Reason: {reason}. Entering setup mode")
        setup_mode()
        server.run()

    else:
        print("Settings loaded successfully.")
      
    # Settings files loaded OK, start up  
    # Display P&L Logo
    print("Displaying logo")
    display.fill(rgb888_to_rgb565(255, 254, 140))

    image_path = "/icons/pl_logo_sparse_gryscl.raw"
    draw_sparse_grayscale(display, image_path)

    time.sleep(3)
    
    # Try to connect to Wifi
    wifi_current_attempt = 1
    while (wifi_current_attempt < WIFI_MAX_ATTEMPTS):
        print(settings['ssid'])
        print(settings['password'])
        print(settings['zip'])
        print(f"Connecting to wifi {settings['ssid']} attempt [{wifi_current_attempt}]")
        
        display.fill(color565(0, 0, 0))
        center_smtext("Connecting to", 40, color565(173, 216, 230))
        center_smtext("WiFi Network SSID:", 60, color565(173, 216, 230))
        center_lgtext(f"{settings['ssid']}", 100, color565(255, 255, 0))
        ip_address = connect_to_wifi(settings["ssid"], settings["password"])
        if is_connected_to_wifi():
            print(f"Connected to wifi, IP address {ip_address}")
                
            display.fill(color565(0, 0, 0))
            center_lgtext("Peony & Lemon",60, color565(255, 254, 140))
            center_lgtext("Forecaster",80, color565(255, 254, 140))
            center_smtext(f"v{__version__}",100)
            center_smtext("Connected:", 120, color565(173, 216, 230))
            center_smtext(f"WiFi SSID: {settings['ssid']}", 140, color565(173, 216, 230))
            center_smtext(f"This IP: {ip_address}", 160, color565(173, 216, 230))
            center_smtext(f"Zip Code: {settings['zip']}", 180)

            time.sleep(1)
            break
        
        else:
            wifi_current_attempt += 1
                
    if is_connected_to_wifi():
#        zip_code = settings["zip"]
#        application_mode(zip_code)
        if not settings.get("lat") or not settings.get("lon"):
            zip_code = settings.get("zip", "").strip()
            lat, lon, reason = get_lat_lon(zip_code)
            if lat is not None and lon is not None:
                settings["lat"] = lat
                settings["lon"] = lon
                save_settings(settings)
            else:
                print("Lat/lon lookup failed:", reason)
                
                # Show error on display and go to setup mode
                display.fill(0)
                center_lgtext("Location Error", 80)
                center_smtext(reason, 100)
                center_smtext("Going to Setup Mode", 120)
                for count in range(5,0, -1):   # Count down from 5 to 1
                    display.fill_rect(0, 140, 240, 16, color565(0, 0, 0))  # Clears 1 text line
                    center_smtext(f"in {count} seconds", 160)
                    time.sleep(1)

                setup_mode()
                server.run()
                
        application_mode(settings)

    else:
        # Bad configuration, reboot
        # into setup mode to get new credentials from the user.
        wlan = network.WLAN(network.STA_IF)
        status = wlan.status()

        msg = f"Error (Code: {status})"
            
        # Display Wifi connect failed message and error
        display.fill(color565(0, 0, 0))
        center_smtext("WiFi Connect Failed:", 80)
        center_smtext(msg,100)
        center_smtext("Going to Setup", 120)
        for count in range(5,0, -1):   # Count down from 5 to 1
            display.fill_rect(0, 140, 240, 16, color565(0, 0, 0))  # Clears 1 text line
            center_smtext(f"in {count} seconds", 140)
            time.sleep(1)
        #Print wifi connect error to console
        print(f"Wifi connect failed {msg}")
        # Log wifi connect error to log file
#         logging.error(f"Wi-Fi connect failed: {msg} (status code: {status})")
        print("Going to setup mode due to Wi-Fi failure.")
        setup_mode()
        server.run()

except Exception as e:
    # Log the error
    buf = uio.StringIO()
    sys.print_exception(e, buf)
#     logging.exception(buf.getvalue())
    
#     logging.info("Restarting device in 2 seconds...")
    time.sleep(2)
    machine.reset()
    
