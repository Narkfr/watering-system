import ujson as json
import uasyncio as asyncio
import usocket as socket
import ure as re
from machine import Pin
from network import WLAN, AP_IF
import time
import os
import _thread

# --- PIN SETUP (BCM logic level inversion possible)
PUMP = Pin(2, Pin.OUT)
VALVES = [Pin(3, Pin.OUT), Pin(4, Pin.OUT), Pin(5, Pin.OUT)]

# --- INITIAL STATE
PUMP.value(1)
for v in VALVES:
    v.value(1)

# --- GLOBAL VARIABLES

VALVES_STATE = {"current": None, "time": "00:00"}
SERVER_SOCKET = None
CYCLE_RUNNING = False

# --- FILES
CONFIG_FILE = "config.json"
CYCLES_FILE = "cycles.json"

# --- HOTSPOT SETUP
def setup_ap():
    ap = WLAN(AP_IF)
    ap.config(essid="PicoArrosage", password="12345678")
    ap.active(True)
    while not ap.active():
        pass
    print("AP actif :", ap.ifconfig())

# --- INIT FILES
if CONFIG_FILE not in os.listdir():
    with open(CONFIG_FILE, "w") as f:
        json.dump({"time": "00:00"}, f)

if CYCLES_FILE not in os.listdir():
    with open(CYCLES_FILE, "w") as f:
        json.dump([], f)

# --- CONFIG TIME
def set_time_str(t_str):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"time": t_str}, f)

def get_time_str():
    with open(CONFIG_FILE) as f:
        return json.load(f)["time"]

# --- CYCLES MANAGEMENT
def add_cycle(heure, d1, d2, d3):
    with open(CYCLES_FILE) as f:
        cycles = json.load(f)
    next_id = max([c["id"] for c in cycles], default=0) + 1
    cycles.append({
        "id": next_id,
        "heure": heure,
        "vanne1_duration": d1,
        "vanne2_duration": d2,
        "vanne3_duration": d3,
        "actif": 1
    })
    with open(CYCLES_FILE, "w") as f:
        json.dump(cycles, f)

def get_active_cycles():
    with open(CYCLES_FILE) as f:
        return [c for c in json.load(f) if c.get("actif", 1) == 1]

def disable_cycle(cycle_id):
    with open(CYCLES_FILE) as f:
        cycles = json.load(f)
    for c in cycles:
        if c["id"] == cycle_id:
            c["actif"] = 0
    with open(CYCLES_FILE, "w") as f:
        json.dump(cycles, f)

def delete_cycle(cycle_id):
    with open(CYCLES_FILE) as f:
        cycles = json.load(f)
    cycles = [c for c in cycles if c["id"] != cycle_id]
    with open(CYCLES_FILE, "w") as f:
        json.dump(cycles, f)

# --- VALVES CONTROL
def open_valve(index, duration_sec):
    print(f"Open valve {index+1} for {duration_sec} sec")
    PUMP.value(0)
    VALVES[index].value(0)
    time.sleep(duration_sec)
    VALVES[index].value(1)
    PUMP.value(1)

# --- SCHEDULER
def scheduler_loop():
    global CYCLE_RUNNING

    while True:
        if not CYCLE_RUNNING:
            now = time.localtime()
            heure_actuelle = f"{now[3]:02}:{now[4]:02}"

            # Lire les cycles actifs depuis le fichier JSON
            with open("cycles.json", "r") as f:
                cycles = json.load(f)

            # Filtrer les cycles actifs correspondant à l'heure actuelle
            matching_cycles = [c for c in cycles if c.get("actif", 1) == 1 and c.get("heure") == heure_actuelle]

            for cycle in matching_cycles:
                print(f"Triggering cycle {cycle['id']} at {heure_actuelle}")
                run_cycle((
                    cycle["id"],
                    cycle["heure"],
                    cycle["vanne1_duration"],
                    cycle["vanne2_duration"],
                    cycle["vanne3_duration"]
                ))

        time.sleep(30)

def run_cycle(cycle):
    global VALVES_STATE, CYCLE_RUNNING
    CYCLE_RUNNING = True

    # cycle = (id, heure, v1, v2, v3)
    v_durations = [cycle[2], cycle[3], cycle[4]]

    for i in range(3):
        print(f"Ouverture vanne {i+1} pendant {v_durations[i]}s")
        VALVES_STATE["current"] = i + 1
        open_valve(i, v_durations[i])
        time.sleep(1)

    VALVES_STATE["current"] = None
    CYCLE_RUNNING = False

# --- WEB SERVER
# Read the content of a file
def read_file(path):
    with open(path, 'r') as f:
        return f.read()

# Read cycles from the JSON file
def read_cycles():
    if CYCLES_FILE not in os.listdir():
        return []  # If the file doesn't exist, return an empty list
    with open(CYCLES_FILE, "r") as f:
        return json.load(f)

# Save the cycles to the JSON file
def save_cycles(cycles):
    with open(CYCLES_FILE, "w") as f:
        json.dump(cycles, f)

# Handle HTTP requests
def handle_request(client):
    request = client.recv(1024).decode()
    lines = request.split("\r\n")
    first_line = lines[0]
    method, path, _ = first_line.split(" ")
    cycles = read_cycles()

    print(f"[HTTP] {method} {path}")

    # ROUTES
    if path == "/" and method == "GET":
        html = read_file("web/index.html")
        send_response(client, html, "text/html")

    elif path == "/api/state" and method == "GET":
        now = time.localtime()
        h = f"{now[3]:02}:{now[4]:02}"
        send_json(client, {"heure": h, "vanne": VALVES_STATE["current"]})

    elif path == "/api/cycles" and method == "GET":
        send_json(client, cycles)

    elif path == "/api/add_cycle" and method == "POST":
        body = request.split("\r\n\r\n")[1]
        data = json.loads(body)
        new_cycle = {
            "id": len(cycles) + 1,
            "heure": data["heure"],
            "vanne1_duration": data["v1"],
            "vanne2_duration": data["v2"],
            "vanne3_duration": data["v3"],
            "actif": 1
        }
        cycles.append(new_cycle)
        save_cycles(cycles)
        send_json(client, {"status": "ok"})

    elif re.match("^/api/cycle/\\d+/pause$", path) and method == "POST":
        index = int(path.split("/")[-2])
        if 0 <= index < len(cycles):
            disable_cycle(index)
            send_json(client, {"status": "cycle mis en pause"})
        else:
            send_json(client, {"error": "index invalide"})

    elif re.match("^/api/cycle/\\d+/delete$", path) and method == "POST":
        index = int(path.split("/")[-2])
        if 0 <= index < len(cycles):
            delete_cycle(index)
            send_json(client, {"status": "cycle supprimé"})
        else:
            send_json(client, {"error": "index invalide"})

    elif re.match("^/api/valve/\\d$", path) and method == "POST":
        index = int(path.split("/")[-1])
        VALVES_STATE["current"] = index + 1
        print(VALVES_STATE["current"])
        open_valve(index, 10)
        send_json(client, {"status": f"valve {index+1} open"})

    else:
        send_response(client, "Not found", "text/plain", code="404 Not Found")

    client.close()

# Send an HTTP response
def send_response(client, content, content_type, code="200 OK"):
    client.send("HTTP/1.1 {}\r\n".format(code))
    client.send("Content-Type: {}\r\n".format(content_type))
    client.send("Access-Control-Allow-Origin: *\r\n")
    client.send("Connection: close\r\n\r\n")
    client.sendall(content)

# Send a JSON response
def send_json(client, obj):
    content = json.dumps(obj)
    send_response(client, content, "application/json")

# Start the server
def start_server():
    global SERVER_SOCKET

    if SERVER_SOCKET:
        SERVER_SOCKET.close()  # Close previous socket if exists

    addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
    SERVER_SOCKET = socket.socket()
    SERVER_SOCKET.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # optional but helpful
    SERVER_SOCKET.bind(addr)
    SERVER_SOCKET.listen(1)
    print("Server listening on port 80")

    while True:
        cl, addr = SERVER_SOCKET.accept()
        print(f"[CONNECTION] From {addr}")
        handle_request(cl)

# --- MAIN
def main():
    setup_ap()
    _thread.start_new_thread(scheduler_loop, ())
    start_server()
    loop = asyncio.get_event_loop()
    loop.create_task(scheduler_loop())
    loop.run_forever()

main()
