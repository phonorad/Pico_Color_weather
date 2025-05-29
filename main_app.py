import network
import urequests
import time
import machine
from machine import Pin, SPI
import math
import ntptime
import gc
import framebuf
import uio
import sys
from phew import access_point, connect_to_wifi, is_connected_to_wifi, dns, server
from phew.template import render_template
from phew import logging
from phew.server import Response
import ujson as json
import os
import ure  # MicroPython’s regex module
import _thread
import socket # temporary for troubleshooting
# Imports for round color tft display
import gc9a01py as gc9a01
import vga1_8x16 as font_sm
import vga1_16x16 as font_lg
import vga1_16x32 as font_huge

# === Software Version ===
__version__ = "1.0.0"
# ========================

# === Definitons for Wifi Setup and Access ===
AP_NAME = "pico weather"
AP_DOMAIN = "picoweather.net"
AP_TEMPLATE_PATH = "ap_templates"
APP_TEMPLATE_PATH = "app_templates"
SETTINGS_FILE = "settings.json"
WIFI_MAX_ATTEMPTS = 3

# === Initialize/define parameters ===
SYNC_INTERVAL = 3600 # Sync to NTP time server every hour
WEATH_INTERVAL = 300 # Update weather every 5 mins
last_sync = 0
last_weather_update = 0
press_time = None
long_press_triggered = False
start_update_requested = False
continue_requested = False
init_complete = False      # Indicate whether all initi is completed (lat lon, gmt offset, weather)
gmt_offset_complete = False
lat_lon_complete = False
weath_setup_complete = False

UPLOAD_TEMP_SUFFIX = ".tmp"

# === Define Months ===
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# === Define timezone ===
gmt_offset = 0   # Initialze gmt offset
#gmt_offset = -4 * 3600  # For EDT (UTC-4), or -5*3600 for EST (UTC-5)

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

# === AP and Wi-Fi Setup ===

SETTINGS_FILE = "settings.json"  # Or "/config/settings.json" if in subdirectory

def load_settings():
    # Case 1: File missing
    if SETTINGS_FILE not in os.listdir():
        print("Settings file is missing.")
        return "missing", None

    try:
        # Try to parse JSON
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)

        # Validate required keys
        required_keys = ["ssid", "password", "zip"]
        for key in required_keys:
            if key not in settings or not settings[key]:
                print(f"Invalid settings: Missing or empty '{key}'")
                return "invalid", None

        return "valid", settings

    except Exception as e:
        # Case 2: File exists but is corrupted or malformed
        import uio
        import sys
        import logging

        buf = uio.StringIO()
        sys.print_exception(e, buf)
        logging.exception("Settings file error:\n" + buf.getvalue())

        return "corrupt", None
    
def machine_reset():
    time.sleep(2)
    print("Resetting...")
    machine.reset()

def setup_mode():
    print("Entering setup mode...")
    display.fill(color565(0, 0, 0))
    center_lgtext("Setup Mode",40, color565(255, 255, 0))
    center_lgtext("On Phone or", 60)
    center_lgtext("Computer Go To", 80)
    center_lgtext("WiFi Settings", 100)
    center_lgtext("and Select", 120)
    center_lgtext("Network:", 140)
    center_lgtext("Pico Weather", 160, color565(0, 128, 128))

    def ap_index(request):
        if request.headers.get("host").lower() != AP_DOMAIN.lower():
            return render_template(f"{AP_TEMPLATE_PATH}/redirect.html", domain = AP_DOMAIN.lower())

        return render_template(f"{AP_TEMPLATE_PATH}/index_wifi_zip.html")

    def ap_configure(request):
        print("Saving wifi and zip credentials...")

        with open(SETTINGS_FILE, "w") as f:
            json.dump(request.form, f)
            f.close()

        # Reboot from new thread after we have responded to the user.
        _thread.start_new_thread(machine_reset, ())
        return render_template(f"{AP_TEMPLATE_PATH}/configured.html", ssid = request.form["ssid"])
        
    def ap_catch_all(request):
        if request.headers.get("host") != AP_DOMAIN:
            return render_template(f"{AP_TEMPLATE_PATH}/redirect.html", domain = AP_DOMAIN)

        return "Not found.", 404

    server.add_route("/", handler = ap_index, methods = ["GET"])
    server.add_route("/configure", handler = ap_configure, methods = ["POST"])
    server.set_callback(ap_catch_all)

    ap = access_point(AP_NAME)
    ip = ap.ifconfig()[0]
    dns.run_catchall(ip)

def start_update_mode():
    print("starting update mode")
    ip = network.WLAN(network.STA_IF).ifconfig()[0]
    print(f"start_update_mode: got IP = {ip}")
    
    display.fill(color565(0, 0, 0))
    center_lgtext("SW Update Mode",80)
    center_lgtext("Enter", 100)
    center_smtext(f"http://{ip}/swup", 100)
    center_lgtext("into broswer", 140)

    def ap_version(request):
        # Return the version defined in main.py
        return Response(__version__, status=200, headers={"Content-Type": "text/plain"})

    def swup_handler(request):
        # Serve your software update HTML page here
        return render_template(f"{AP_TEMPLATE_PATH}/index_swup_git.html")

    def favicon_handler(request):
        return Response("", status=204)  # No Content

    def continue_handler(request):
        global continue_requested
        continue_requested = True
        print("Continue requested, restarting device...")
        # Schedule reboot after response is sent
        # Start a delayed reset thread to allow HTTP response to complete
        
        def delayed_restart():
            time.sleep(1)  # Wait ~1s to let HTTP response flush
            machine_reset()

        _thread.start_new_thread(machine_reset, ())
        return Response("Restarting device...", status=200, headers={"Content-Type": "text/plain"})

    async def upload_handler(request):
        filename = request.query.get("filename")
        if not filename:
            return Response("Missing filename", status=400)

        try:
            total_written = 0
            chunk_size = 1024

            with open(filename, "wb") as f:
                while True:
                    chunk = await request.read_body_chunk(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    total_written += len(chunk)

            return Response(f"Saved {total_written} bytes to {filename}", status=200)

        except Exception as e:
            return Response(f"Error: {e}", status=500)
        
    def catch_all_handler(request):
        print(f"Fallback route hit: {request.method} {request.path}")
        return Response("Route not found", status=404)
        
    server.add_route("/swup", handler=swup_handler, methods=["GET"])
    server.add_route("/version", handler=ap_version, methods=["GET"])
    server.add_route("/favicon.ico", handler=favicon_handler, methods=["GET"])
    server.add_route("/continue", handler=continue_handler, methods=["POST"])
    server.add_route("/upload", handler=upload_handler, methods=["POST"])
        
    # Start the server (if not already running)
    print(f"Waiting for user at http://{ip}/swup ...")
    server.run()

    # Wait until user clicks OK
    while not continue_requested:
        time.sleep(0.1)

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
        
def is_daytime():
#    t = time.localtime()
    t = localtime_with_offset()
    hour = t[3]  # Hour is the 4th element in the tuple
    return 7 <= hour < 19  # Define day as between 7am and 7pm (0700 to 1900)

def localtime_with_offset():
    """
    Return local time.struct_time adjusted from UTC using raw_offset and DST.
    DST logic is based on US rules.
    """
    now = time.gmtime()
    month = now[1]
    mday = now[2]
    weekday = now[6]  # 0 = Monday, 6 = Sunday

    def is_us_dst(month, mday, weekday):
        if month < 3 or month > 11:
            return False
        if 3 < month < 11:
            return True
        if month == 3:
            return mday - weekday >= 8  # 2nd Sunday or later
        if month == 11:
            return mday - weekday < 1  # before 1st Sunday
        return False

    if gmt_offset:          # Make sure gmt_offset is not None
        offset = gmt_offset
    else:
        offset = 0
        
    if is_us_dst(month, mday, weekday):
        offset += 1  # apply DST

    t = time.mktime(time.gmtime()) + int(offset * 3600)
    return time.localtime(t)

def update_time_only(time_str):
    display.fill_rect(0, 40, 240, 20, color565(0, 0, 0))  # Clear just time area
    center_lgtext(time_str, 40, color565(0, 255, 255))
    
def update_date_only(date_str):
    display.fill_rect(0, 20, 240, 20, color565(0, 0, 0))  # Clear just date area
    center_lgtext(date_str, 20, color565(255, 255, 255))    

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

def get_icon_filename(simplified_now, day):
    f = simplified_now.lower()
    print(f"simplified forecast: {f}")

    icon_filename = None

    if "sun" in f or "clear" in f:
        icon_filename = "icons/clear_day_rgb565.raw" if day else "icons/clear_night_rgb565.raw"
    elif "partly cloudy" in f or "mostly cloudy" in f or "p cloudy" in f or "m cloudy" in f:
        icon_filename = "icons/part_cloudy_day_rgb565.raw" if day else "icons/part_cloudy_night_rgb565.raw"
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
        icon_filename = "icons/clear_day_rgb565.raw"
    
    print(f"Icon filename selected: {icon_filename}")
    return icon_filename


def draw_weather_icon(gc9a01, simplified_now, x, y):
#    gc9a01.fill_rect(x, y, 48, 32, 0)
    day = is_daytime()

    icon_filename = get_icon_filename(simplified_now, day)
    if icon_filename:
        try:
            with open(icon_filename, "rb") as f:
                icon_data = f.read()

            # Scale icon 2x larger
#            scaled_icon_data, sw, sh = scale_rgb565_2x(icon_data, 32, 32)

            # Apply color to icon
#            WHITE = 0xFFFF
#            NEW_COLOR = rgb888_to_rgb565(100, 200, 255)  # Light blue
#            colored_icon = replace_color_rgb565(scaled_icon_data, WHITE, NEW_COLOR)

            gc9a01.blit_buffer(icon_data, x, y, 64, 64)

        except OSError:
            gc9a01.text(font_lg, "Err", x, y, color565(255, 0, 0))
    else:
        gc9a01.text(font_lg, "N/A", x, y, color565(255, 0, 0))
        
# === Scale up weather icon ===
def scale_rgb565_2x(src_bytes, width, height):
    # src_bytes length should be width*height*2 bytes (2 bytes per pixel)
    scaled_width = width * 2
    scaled_height = height * 2
    scaled_bytes = bytearray(scaled_width * scaled_height * 2)

    for y in range(height):
        for x in range(width):
            # read pixel (2 bytes)
            index = (y * width + x) * 2
            pixel_hi = src_bytes[index]
            pixel_lo = src_bytes[index + 1]

            # replicate pixel to 2x2 block in scaled_bytes
            for dy in range(2):
                for dx in range(2):
                    sx = x * 2 + dx
                    sy = y * 2 + dy
                    scaled_index = (sy * scaled_width + sx) * 2
                    scaled_bytes[scaled_index] = pixel_hi
                    scaled_bytes[scaled_index + 1] = pixel_lo

    return scaled_bytes, scaled_width, scaled_height

# === Drawing ===

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

def display_raw_image_in_chunks(display, filepath, x, y, width, height, chunk_rows=8, clear_color=0x0000, clear=True):
    """
    Streams a raw RGB565 image to the GC9A01 display in chunks using blit_buffer().

    Args:
        display:     Initialized GC9A01 display object.
        filepath:    Path to the .raw RGB565 image file.
        x, y:        Top-left position on the screen to draw the image.
        width:       Width of the image in pixels.
        height:      Height of the image in pixels.
        chunk_rows:  Number of rows per chunk (default: 8).
        clear_color: Optional background color (default: black).
        clear:       If True, clear the screen before drawing.
    """
    import gc

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

                display.blit_buffer(chunk_data, x, y + row_start, width, actual_rows)

                gc.collect()

    except Exception as e:
        print("Error displaying image:", e)

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
            return lat, lon
        else:
            print("Lat/Lon API response error:", response.status_code)
    except Exception as e:
        print("Failed to get lat/lon:", e)
    return None, None

def get_gmt_offset(lat, lon, username="phonorad"):
    try:
        url = f"http://api.geonames.org/timezoneJSON?lat={lat}&lng={lon}&username={username}"
        response = urequests.get(url)
        if response.status_code == 200:
            data = response.json()
            gmt_offset = data.get("gmtOffset")
            print(data)
            print(f"GMT Offset: {gmt_offset} hours")
            return gmt_offset
        else:
            print(f"Timezone API response error: {response.status_code}")
    except Exception as e:
        print(f"Failed to get GMT offset: {e}")
    return None

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
    key_bytes = b'"' + key.encode("utf-8") + b'":"'
    buf = b""
    max_buf = 1024

    while True:
        chunk = response_stream.read(128)
        if not chunk:
            break
        buf += chunk
        if len(buf) > max_buf:
            buf = buf[-max_buf:]
        idx = buf.find(key_bytes)
        if idx != -1:
            start = idx + len(key_bytes)
            end = buf.find(b'"', start)
            if end != -1:
                return buf[start:end].decode("utf-8")
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

# === Weather Setup ===
#LAT = 41.4815
#LON = -73.2132
USER_AGENT = "PicoWeatherDisplay (contact@example.com)"  # replace with your info

def get_weather_data(lat, lon):
    try:
        headers = {"User-Agent": USER_AGENT}

        # Step 1: Get point data - forecast and observation stations
        print("fetching URL:", f"https://api.weather.gov/points/{lat},{lon}")
        r = urequests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers)
        raw = r.text
        print("Downloaded length:", len(raw))  # Debug: size of JSON string
        r.close()

        # Parse full JSON only after raw text is safely loaded
        point_data = json.loads(raw)
        
        print("Keys in point_data:", list(point_data.keys()))  # Debug keys in JSON

        # Extract only the needed URLs to minimize retained data in memory
        forecast_url = point_data["properties"]["forecast"]
        obs_station_url = point_data["properties"]["observationStations"]
        forecast_hourly_url = point_data["properties"].get("forecastHourly")
        
        # Directly extract grid identifiers for constructing hourly URLs
        office = point_data["properties"]["gridId"]
        grid_x = point_data["properties"]["gridX"]
        grid_y = point_data["properties"]["gridY"]

        # Build the base gridpoint URL
        gridpoint_url      = f"https://api.weather.gov/gridpoints/{office}/{grid_x},{grid_y}"
        
        # Choose the best available hourly forecast URL
        if not forecast_hourly_url:
            print("No forecastHourly URL found in point data; falling back to constructed gridpoint URL.")
            hourly_url = gridpoint_url + "/forecast/hourly"
        else:
            hourly_url = forecast_hourly_url  # NOTE: Use hourly_url, NOT gridpoint or forecast_hourly
        
        # Clean up the large JSON object ASAP
        del point_data
        gc.collect()

        # Step 2: Get observation stations list for the location
        print("Fetching URL:", obs_station_url)

        # Use the helper to extract the first stationIdentifier
        station_id = fetch_first_station_id(obs_station_url, headers)
        r.close
        gc.collect()
        # If not found, return None to indicate failure
        if not station_id:
            temp_c = humidity = None
            return None

        # Free memory
        del raw
        gc.collect()

        # Step 3 - Fetch latest observations
        temp_c = None
        temp_f = None
        humidity = None

        station_url =  f"https://api.weather.gov/stations/{station_id}/observations/latest"
        print("Fetching URL:", station_url)

        try:
            r = urequests.get(station_url, headers=headers)
            raw = r.text
            print("Downloaded length (obs):", len(raw))
            r.close()

            obs_json = json.loads(raw)

            temp_c = obs_json["properties"]["temperature"]["value"]
            print("Parsed Temperature (°C):", temp_c)

            humidity = obs_json["properties"]["relativeHumidity"]["value"]
            print("Parsed Humidity (%):", humidity)

            del raw
            del obs_json
            gc.collect()

            if temp_c is not None:
                temp_f = round(temp_c * 9 / 5 + 32)

        except Exception as e:
            print("Error fetching or parsing observation data:", e)

        # Fallback to hourly forecast if needed
        
        # +++ TEST ++++
        #temp_c = None  # Set to None to test hourly_forecast fetch
        #humidity = None # Set to None to test hourly_forecast fetch
        # +++ TEST ++++
        
        if temp_c is None or humidity is None:
            print("Falling back to hourly forecast for missing data...")
            print("Fetching URL:", hourly_url)
            
            # Pattern to match a flat "temperature": 72  (first occurrence)
            pattern_temp = rb'"temperature"\s*:\s*([0-9]+(?:\.[0-9]+)?)'
 
            try:
                r = urequests.get(hourly_url, headers=headers)
                temp_f_fb = extract_first_number_stream_generic(r.raw, pattern_temp)
                r.close()
                gc.collect()
                print("Stream-fallback Temp (°F):", temp_f_fb)

                if temp_c is None:
                    if isinstance(temp_f_fb, float):
                        temp_f = round(temp_f_fb)
                        temp_c = round((temp_f_fb - 32) * 5 / 9)
                    else:
                        temp_f = 0
                        temp_c = 0
                        print("Stream fallback didn’t find valid temperature; defaulting to 0.")

            except Exception as e:
                print("Error streaming temperature fallback:", e)
                temp_c = temp_f = 0

            # Pattern to match the nested "relativeHumidity": { … "value": 54 }
            pattern_hum = rb'"relativeHumidity"\s*:\s*\{[^}]*"value"\s*:\s*([0-9]+(?:\.[0-9]+)?)'

            try:
                # --- Humidity fallback (%) ---
                r = urequests.get(hourly_url, headers=headers)
                humidity_fb = extract_first_number_stream_generic(r.raw, pattern_hum)
                r.close()
                gc.collect()
                print("Stream-fallback Humidity (%):", humidity_fb)
                
                if humidity is None:
                    if isinstance(humidity_fb, float):
                        humidity = int(humidity_fb)
                    else:
                        print("Stream fallback didn’t find valid humidity; defaulting to 0.")
                        humidity = 0

            except Exception as e:
                print("Error streaming humidity fallback:", e)
                humidity = 0

        # Final safety check
        if temp_f is None:
            temp_f = 0
        if humidity is None:
            humidity = 0

        # Step 4: Get forecast data
        print("Fetching URL:", forecast_url)
        r = urequests.get(forecast_url, headers=headers)
        raw_forecast = r.text
        print("Downloaded length (forecast):", len(raw_forecast))
        r.close()

        # Extract shortForecast from first forecast period
        forecast = extract_first_json_string_value(raw_forecast, "shortForecast")
        if not forecast:
            forecast = "N/A"

        # Free memory from forecast JSON
        del raw_forecast
        gc.collect()

        # Return the final values
        return temp_f, humidity, forecast

    except Exception as e:
        print("Error:", e)
        sys.print_exception(e)
        return None


def simplify_forecast(forecast):
    MODIFIERS = ["Slight Chance", "Chance", "Mostly", "Partly", "Likely", "Scattered", "Isolated"]
    CONDITIONS = [
        "Tornado", "Hailstorm", "Hailstorms", "Blizzard", "Winter Storm", "Winter Weather"
        "Freezing Rain", "Freezing Drizzle", "Hail", "Sleet", "Ice", "Frost",
        "Flash Flood", "Flood", "Dust Storm", "Smoke", "Volcanic Ash",
        "Hurricane", "Tropical storm", "Thunderstorm",
        "Thunderstorms", "T-storms", "Tstorms",
        "Storm", "Showers", "Rain",
        "Fog", "Snow", "Clear", "Sunny",
        "Cloudy", "Windy", "Gusty", "Wind", "Drizzle",
        "Haze"
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
        # Next check conditions and make 7 chars or less
        if found_condition.lower() =="hailstorm":
            found_condition = "Hailstrm"
        if found_condition.lower() =="hailstorms":
            found_condition = "Hailstrm"
        if found_condition.lower() =="blizzard":
            found_condition = "Blizzrd"
        if found_condition.lower() =="winter storm":
            found_condition = "Wint St"
        if found_condition.lower() =="winter weather":
            found_condition = "Wint Wth"
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

    
def display_weather(temp, humidity, forecast):
    # Clear only the areas we'll update (not the whole screen)
#     display.fill_rect(0, 0, 240, 60, color565(0, 0, 0))     # header
    display.fill_rect(0, 60, 240, 180, color565(0, 0, 0))   # lower part
    
    line = simplify_forecast(forecast)
    icon_x = (240 - 64) // 2
    draw_weather_icon(display, line, icon_x, 70)    
    # Display 14 character weather conditions
    
    center_lgtext(line, 140, color565(255, 255, 0))

    display.text(font_huge, f"{temp}F", 50, 170, color565(255, 100, 100))  # pass in 8-bit RGB
    #display.text(font_huge, f"{temp}F", 50, 170, 0xfa45)  # Pass in rgb565
    display.text(font_huge, f"{int(humidity)}%", 130, 170, color565(100, 255, 100))
    
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
    # Return H:M:S AM/PM
#    return "{:2d}:{:02d}:{:02d} {}".format(hour_12, t[4], t[5], am_pm)
    # Return H:M AM/PM
    return "{:2d}:{:02d} {}".format(hour_12, t[4], am_pm)
# === Weather Program ===
def application_mode(zip_code):
    print("Entering application mode.")
    global start_update_requested
    global gmt_offset
#    onboard_led = machine.Pin("LED", machine.Pin.OUT)
#    setup_wifi_sw = machine.Pin(5, machine.Pin.IN)


    # Initial time sync
    sync_time()
    last_sync = time.time()
    last_weather_update = last_sync
    temp = humidity = forecast = None
    last_displayed_time = ""
    last_displayed_date = ""
    
    # Determine Latitude and Longitude
    lat, lon = get_lat_lon(zip_code)
    lat_lon_complete = lat is not None and lon is not None
    print("Latitude:", lat)
    print("Longitude:", lon)
    
    # Get time zone UTC offset from lat and lon
    gmt_offset = get_gmt_offset(lat, lon)
    if gmt_offset is None:
        gmt_offset = 0  # fallback to UTC
        gmt_offset_complete = False
    else:
        gmt_offset_complete = True

    # Initial weather fetch
    new_data = get_weather_data(lat, lon)
    if new_data:
        temp, humidity, forecast = new_data
        print(f"Updated: Temp: {temp}F, Humidity: {humidity}%, Forecast: {forecast}")
        display_weather(temp, humidity, forecast)
    else:
        temp, humidity, forecast = None, None, None
        display.fill(color565(0, 0, 0))
        center_lgtext("Weather data", 80)
        center_lgtext("unavailable", 100)

    last_weather_update = time.time()

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
    
        # Refresh weather WEATH_INTERVAL (5 min/300 sec) 
        if current_time - last_weather_update >= WEATH_INTERVAL:
            if not lat_lon_complete:
                lat, lon = get_lat_lon(zip_code)
                lat_lon_complete = lat is not None and lon is not None
                if lat_lon_complete:
                    print(f"Got lat/lon: {lat}, {lon}")
                
            # Retry gmt offset if gmt offset not yet obtained
            if not gmt_offset_complete:
                gmt_offset = get_gmt_offset(lat, lon)
                gmt_offset_complete = gmt_offset is not None
                if gmt_offset_complete:
                    print(f"Got GMT offset: {gmt_offset}")
                
            if lat_lon_complete:     
                new_data = get_weather_data(lat, lon)
                if new_data:
                    temp, humidity, forecast = new_data
                    print(f"Updated: Temp: {temp}F, Humidity: {humidity}%, Forecast: {forecast}")
                    display_weather(temp, humidity, forecast)
                else:
                    temp, humidity, forecast = None, None, None
                    display.fill_rect(0, 60, 240, 180, color565(0, 0, 0)) # x, y, w, h
                    center_lgtext("Weather Data", 80)
                    center_lgtext("Unavailable", 100)
                last_weather_update = current_time

        # Get localtime *once* per loop
        now = localtime_with_offset()
        current_time_str = format_12h_time(now)
        current_date_str = "{} {:02}".format(MONTHS[now[1]-1], now[2])
        
        # Update time display every second
        if current_time_str != last_displayed_time:
            update_time_only(current_time_str)
            last_displayed_time = current_time_str
            
        # Optional: update date only when it changes
        if current_date_str != last_displayed_date:
            update_date_only(current_date_str)
            last_displayed_date = current_date_str

        time.sleep(0.1)  # Short sleep to maintain responsiveness

#        time.sleep(1)
    
# === Main Program - Connnect to Wifi or goto AP mode Wifi setup ===
# ===                If Wifi connection OK, go to Weather program ===
# Figure out which mode to start up in...
try:
    # See if setup wifi switch is pressed
    if setup_sw.value() == False:
        t = 50  # Switch must be pressed for 5 seconds to reset wifi config
        while setup_sw.value() == False and t > 0:
            t -= 1
            time.sleep(0.1)
        if setup_sw.value() == False:
            print("Setup switch ")
            os.remove(SETTINGS_FILE)
            machine_reset()
            
    # See if settings.txt is there and valid
    status, settings = load_settings()
    if status == "missing":
        # Display no settings file message and go to initial setup
        display.fill(color565(0, 0, 0))
        center_smtext("No Settings File Found", 80)
        center_smtext("Going to Initial Setup Screen", 120)
        for count in range(5,0, -1):   # Count down from 5 to 1
            display.fill_rect(0, 140, 240, 16, color565(0, 0, 0))  # Clears 1 text line
            center_smtext(f"in {count} seconds", 140)
            time.sleep(1)
        print("Settings file not found. Entering setup mode.")
        setup_mode()
        server.run()
        
    elif status in ("invalid", "corrupt"):
        # Display no settings file message and go to initial setup
        display.fill(color565(0, 0, 0))
        center_smtext("Settings File Invalid", 80)
        center_smtext("Going to Initial Setup Screen", 120)
        for count in range(5,0, -1):   # Count down from 5 to 1
            display.fill_rect(0, 140, 240, 16, color565(0, 0, 0))  # Clears 1 text line
            center_smtext(f"in {count} seconds", 140)
            time.sleep(1)
        print("Settings file invalid or corrupted. Entering Setup Mode")
        # delete file and enter setup mode:
        try:
            os.remove(SETTINGS_FILE)
        except:
            pass
        setup_mode()
        server.run()

    else:
        print("Settings loaded successfully.")
      
    # Settings files loaded OK, start up  
    # Display P&L Logo
    print("Displaying logo")
    image_path = "/icons/pl_logo_240x240_rgb565.raw"
    display_raw_image_in_chunks(display, image_path, 0, 0, 240, 240)
    time.sleep(5)
    
    # TRy to connect to Wifi
    wifi_current_attempt = 1
    while (wifi_current_attempt < WIFI_MAX_ATTEMPTS):
        print(settings['ssid'])
        print(settings['password'])
        print(settings['zip'])
        print(f"Connecting to wifi {settings['ssid']} attempt [{wifi_current_attempt}]")
        
        display.fill(color565(0, 0, 0))
        center_smtext("Connecting to", 120, color565(173, 216, 230))
        center_smtext("WiFi Network SSID:", 140, color565(173, 216, 230))
        center_smtext(f"{settings['ssid']}", 160, color565(173, 216, 230))
        ip_address = connect_to_wifi(settings["ssid"], settings["password"])
        if is_connected_to_wifi():
            print(f"Connected to wifi, IP address {ip_address}")
                
            display.fill(color565(0, 0, 0))
            center_lgtext("Peony & Lemon",60, color565(255, 254, 140))
            center_lgtext("Mini Weather",80, color565(255, 254, 140))
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
        zip_code = settings["zip"]
        application_mode(zip_code)
    else:
        # Bad configuration, delete the credentials file, reboot
        # into setup mode to get new credentials from the user.
        wlan = network.WLAN(network.STA_IF)
        status = wlan.status()

        msg = f"Error (Code: {status})"
            
        # Display Wifi connect failed message and error
        display.fill(color565(0, 0, 0))
        center_smtext("WiFi Connect Failed:", 80)
        center_smtext(msg,100)
        center_smtext("Going to Initial Setup Screen", 120)
        for count in range(5,0, -1):   # Count down from 5 to 1
            display.fill_rect(0, 140, 240, 16, color565(0, 0, 0))  # Clears 1 text line
            center_smtext(f"in {count} seconds", 140)
            time.sleep(1)
        #Print wifi connect error to console
        print(f"❌ {msg}")
        # Log wifi connect error to log file
        logging.error(f"Wi-Fi connect failed: {msg} (status code: {status})")
        time.sleep(5)
        os.remove(SETTINGS_FILE)
        machine_reset()

except Exception as e:
    # Log the error
    buf = uio.StringIO()
    sys.print_exception(e, buf)
    logging.exception(buf.getvalue())
    
    logging.info("Restarting device in 2 seconds...")
    time.sleep(2)
    machine.reset()
    
