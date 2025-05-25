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
import _thread
import socket # temporary for troubleshooting
# Imports for round color tft display
import gc9a01py as gc9a01
import vga1_8x16 as font_sm
import vga1_16x16 as font_lg
import vga1_16x32 as font_huge

# === Software Version ===
__version__ = "1.1.0"
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
start_update_requested = False
continue_requested = False
UPLOAD_TEMP_SUFFIX = ".tmp"

# === Define Months ===
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# === Define timezone ===
UTC_OFFSET = -4 * 3600  # For EDT (UTC-4), or -5*3600 for EST (UTC-5)

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
def machine_reset():
    time.sleep(2)
    print("Resetting...")
    machine.reset()

def setup_mode():
    print("Entering setup mode...")
    display.fill(color565(0, 0, 0))
    center_lgtext("Setup Mode",40)
    center_lgtext("Open browser", 60)
    center_lgtext("SELECT WiFi", 100)
    center_lgtext("Pico Weather", 140)

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
    center_lgtext("SW Update Mode",40)
    center_lgtext("Enter", 60)
    center_lgtext("http://", 100)
    center_lgtext("/swup", 140)
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
        print("📥 Upload handler triggered")
        try:
            filename = request.query.get("filename")
            if not filename:
                return Response("Missing filename", status=400)

            content = request.data

            # Normalize to bytes
            if isinstance(content, str):
                content = content.encode("utf-8")
            elif not isinstance(content, bytes):
                print(f"❌ Unexpected data type: {type(content)}")
                return Response("Unexpected body data type", status=400)

            print(f"✅ Received {len(content)} bytes for {filename}")

            with open(filename, "wb") as f:
                f.write(content)

            print(f"💾 Uploaded file saved: {filename}")
            return Response(f"Saved to {filename}", status=200)

        except Exception as e:
            print(f"❌ Upload error: {e}")
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
    
# === Set correct time from NTP server ===
def sync_time():
    try:
        print("Syncing time with NTP server...")
        ntptime.settime()  # This sets the RTC from the network time
        print("Time synced successfully!")
    except Exception as e:
        print(f"Failed to sync time: {e}")
        
def is_daytime():
#    t = time.localtime()
    t = localtime_with_offset()
    hour = t[3]  # Hour is the 4th element in the tuple
    return 7 <= hour < 19  # Define day as between 7am and 7pm (0700 to 1900)
        
# === Calculate correct local time ===
def localtime_with_offset():
    t = time.mktime(time.localtime())  # seconds since epoch UTC
    t += UTC_OFFSET                    # add offset seconds
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
    elif "partly cloudy" in f or "p cloudy" in f or "m cloudy" in f:
        icon_filename = "icons/part_cloudy_day_rgb565.raw" if day else "icons/part_cloudy_night_rgb565.raw"
    elif "tstorms" in f or "thunderstorm" in f or "thunderstorms" in f or "t-storm" in f:
        icon_filename = "icons/tstorm_rgb565.raw"
    elif "cloud" in f or "overcast" in f:
        icon_filename = "icons/cloudy_rgb565.raw"
    elif "rain" in f or "showers" in f or "drizzle" in f:
        icon_filename = "icons/rain_rgb565.raw"
    elif "fog" in f or "haze" in f:
        icon_filename = "icons/fog_rgb565.raw"
    elif "snow" in f or "flurries" in f or "sleet" in f:
        icon_filename = "icons/snow_rgb565.raw"
    elif "wind" in f:
        icon_filename = "icons/windy_rgb565.raw"

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
            scaled_icon_data, sw, sh = scale_rgb565_2x(icon_data, 32, 32)

            # Apply color to icon
            WHITE = 0xFFFF
            NEW_COLOR = rgb888_to_rgb565(100, 200, 255)  # Light blue
            colored_icon = replace_color_rgb565(scaled_icon_data, WHITE, NEW_COLOR)

            gc9a01.blit_buffer(colored_icon, x, y, sw, sh)

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
            print("API response error:", response.status_code)
    except Exception as e:
        print("Failed to get lat/lon:", e)
    return None, None

# === Weather Setup ===
#LAT = 41.4815
#LON = -73.2132
USER_AGENT = "PicoWeatherDisplay (contact@example.com)"  # replace with your info

def get_weather_data(lat, lon):
    try:
        headers = {"User-Agent": USER_AGENT}

        # Step 1: Get forecast and observation stations
        r = urequests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers)
        point_data = r.json()
        r.close()
        gc.collect()

        forecast_url = point_data["properties"]["forecast"]
        obs_station_url = point_data["properties"]["observationStations"]
        del point_data  # free memory
        gc.collect()

        # Step 2: Get observation stations list
        r = urequests.get(obs_station_url, headers=headers)
        stations_data = r.json()
        r.close()
        gc.collect()

        features = stations_data.get("features", [])
        if not features:
            return None
        station_id = features[0]["properties"]["stationIdentifier"]
        del stations_data, features  # free memory
        gc.collect()

        # Step 3: Get latest observations
        obs_url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
        r = urequests.get(obs_url, headers=headers)
        obs_data = r.json()
        r.close()
        gc.collect()

        obs = obs_data.get("properties", {})
        del obs_data
        gc.collect()

        temp_c = obs.get("temperature", {}).get("value", None)
        humidity = obs.get("relativeHumidity", {}).get("value", None)

        if temp_c is not None:
            temp_f = round(temp_c * 9 / 5 + 32)
        else:
            temp_f = None

        # Get forecast data
        r = urequests.get(forecast_url, headers=headers)
        forecast_data = r.json()
        r.close
        gc.collect()
        
        forecast = "N/A"
        periods = forecast_data.get("properties", {}).get("periods", {})
        if periods:
            forecast = periods[0].get("shortForecast", "N/A")
            
        return temp_f, humidity, forecast

    except Exception as e:
        print("Error:", e)
        return None


def simplify_forecast(forecast):
    MODIFIERS = {
        "mostly": "M",
        "partly": "P",
        "chance": "Chc",
        "slight chance": "Chc"
    }

    CONDITIONS = {
        "thunderstorms": "Tstorms",
        "t-storms": "Tstorms",
        "tstorms": "Tstorms",
        "sunny": "Sunny",
        "cloudy": "Cloudy",
        "rain": "Rain",
        "showers": "Rain",  # simplify Showers to Rain
        "fog": "Fog",
        "snow": "Snow",
        "clear": "Clear",
        "wind": "Wind",
        "drizzle": "Drizzle",
        "storm": "Storm",
        "sleet": "Sleet",
        "haze": "Haze"
    }

    def simplify_phrase(text):
        text = text.lower()
        mod_abbr = ""
        cond_abbr = ""

        # Check for modifiers first - longest first to catch "slight chance"
        for mod_full in sorted(MODIFIERS.keys(), key=len, reverse=True):
            if mod_full in text:
                mod_abbr = MODIFIERS[mod_full]
                break

        # Check for condition keywords
        for cond_full in CONDITIONS:
            if cond_full in text:
                cond_abbr = CONDITIONS[cond_full]
                break

        # If no condition found, fallback to truncated original text capitalized
        if not cond_abbr:
            return text[:8].capitalize()

        # Combine modifier + condition, max 8 chars total
        if mod_abbr:
            combined = f"{mod_abbr} {cond_abbr}"
        else:
            combined = cond_abbr

        return combined[:8]

    # Split forecast into parts by known separators
    separators = [" then ", ";", ","]
    parts = [forecast]

    for sep in separators:
        if sep in forecast.lower():
            parts = [p.strip() for p in forecast.lower().split(sep)]
            break

    # Simplify each part (max 2 parts)
    simplified = [simplify_phrase(p) for p in parts[:2]]

    return simplified
    
def display_weather(temp, humidity, forecast):
    # Clear only the areas we'll update (not the whole screen)
#     display.fill_rect(0, 0, 240, 60, color565(0, 0, 0))     # header
    display.fill_rect(0, 60, 240, 180, color565(0, 0, 0))   # lower part
    
    lines = simplify_forecast(forecast)
    icon_x = (240 - 64) // 2
    draw_weather_icon(display, lines[0], icon_x, 60)    
    if len(lines) == 2:
        center_lgtext(f"Now:{lines[0]}", 130, color565(255, 255, 0))
        center_lgtext(f"Later:{lines[1]}", 150, color565(255, 255, 0))
    else:
        center_lgtext(lines[0], 140, color565(255, 255, 0))

    display.text(font_huge, f"{temp}F", 50, 170, color565(255, 100, 100))
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
    return "{:2d}:{:02d}:{:02d} {}".format(hour_12, t[4], t[5], am_pm)

# === Weather Program ===
def application_mode(zip_code):
    print("Entering application mode.")
    global start_update_requested
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
    print("Latitude:", lat)
    print("Longitude:", lon)

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
#    onboard_led = machine.Pin("LED", machine.Pin.OUT)
#    setup_wifi_sw = machine.Pin(5, machine.Pin.IN, machine.Pin.PULL_UP)
    os.stat(SETTINGS_FILE)
    # File was found, attempt to connect to wifi...
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
    
    with open(SETTINGS_FILE) as f:
        wifi_current_attempt = 1
        settings = json.load(f)
        while (wifi_current_attempt < WIFI_MAX_ATTEMPTS):
            print(settings['ssid'])
            print(settings['password'])
            print(settings['zip'])
            ip_address = connect_to_wifi(settings["ssid"], settings["password"])
            zip_code = settings["zip"]
            if is_connected_to_wifi():
                print(f"Connected to wifi, IP address {ip_address}")
                
                display.fill(color565(0, 0, 0))
                center_lgtext(f"v{__version__}",40)
                center_lgtext("Connect to:", 60)
                center_lgtext(f"{settings['ssid']}", 100)
                center_lgtext(f"{ip_address}", 140)

                time.sleep(2)
                break
            else:
                wifi_current_attempt += 1
                
        if is_connected_to_wifi():
            application_mode(zip_code)
        else:
            # Bad configuration, delete the credentials file, reboot
            # into setup mode to get new credentials from the user.
            print("Bad wifi connection!")
            os.remove(SETTINGS_FILE)
            machine_reset()

except Exception as e:
    # Either no wifi configuration file found, or something went wrong, 
    # so go into setup mode.
    
    # Send exception info to console
    print("Exception occurred:", e)
    
    logging.error("Exception occurred: {}".format(e))

    # Capture traceback into a string and log it
    buf = uio.StringIO()
    sys.print_exception(e, buf)
    logging.exception(buf.getvalue())
    
    setup_mode()
    server.run()

#Start the web server...
#server.run()    
