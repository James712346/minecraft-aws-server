import socket
import struct
import json
import uuid
import random
import time
import boto3
import threading
m
HOST = '0.0.0.0'
PORT = 25565
TARGET_PORT = 25565

instance_id = "i-043b227d8fcfa2cd7"
region = "ap-southeast-2"


# --- AWS Functions ---#

def get_instance_status():
    ec2 = boto3.client("ec2", region_name=region)
    response = ec2.describe_instance_status(InstanceIds=[instance_id], IncludeAllInstances=True)
    if not response["InstanceStatuses"]:
        return "unknown"
    return response["InstanceStatuses"][0]["InstanceState"]["Name"]


def get_instance_ip():
    ec2 = boto3.client("ec2", region_name=region)
    reservations = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    if not reservations or not reservations[0]["Instances"]:
        return None
    return reservations[0]["Instances"][0].get("PublicIpAddress")

def start_instance():
    ec2 = boto3.client("ec2", region_name=region)
    ec2.start_instances(InstanceIds=[instance_id])
    print(f"Starting instance {instance_id}")


# --- Protocol helpers --- #

def flush_socket(sock):
    sock.setblocking(0)
    while 1:
        try:
            sock.recv(1)
        except:
            sock.setblocking(1)
            break

def read_varint(sock):
    value = 0
    shift = 0
    while True:
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("Socket closed during VarInt read")
        byte = byte[0]
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
        if shift > 35:
            raise ValueError("VarInt too big")
    return value

def write_varint(number, max_bits=32):
        """
        Packs a varint.
        """

        number_min = -1 << (max_bits - 1)
        number_max = +1 << (max_bits - 1)
        if not (number_min <= number < number_max):
            raise ValueError(f"varint does not fit in range: {number_min:d} <= {number:d} < {number_max:d}")

        if number < 0:
            number += 1 << 32

        out = b""
        for _ in range(10):
            b = number & 0x7F
            number >>= 7
            out += struct.pack(">B", b| (0x80 if number >0 else 0))
            if number == 0:
                break
        return out

def write_string(s):
    encoded = s.encode("utf-8")
    return write_varint(len(encoded), max_bits=16) + encoded

def send_packet(sock, packet_id, payload):
    data = write_varint(packet_id) + payload
    length = write_varint(len(data))
    sock.sendall(length + data)

# --- Packet senders --- #
def ping_server(start=True):
    if get_instance_status() == "stopped":
        if start:
            start_instance()
        else:
            return None

    address = get_instance_ip()
    port = TARGET_PORT
    print(f"Pinging {address}:{port}...")
    try:
        with socket.create_connection((address, port), timeout=2) as sock:
            # Handshake packet
            host = write_string(address)
            handshake = (
                write_varint(765) +  # Protocol version (765 = 1.20.5/1.21)
                host +
                struct.pack(">H", port) +
                write_varint(1)  # next state = status
            )
            send_packet(sock, 0x00, handshake)

            # Status Request packet
            send_packet(sock, 0x00, b"")

            # Read response
            length = read_varint(sock)
            packet_id = read_varint(sock)
            if packet_id != 0x00:
                return None

            json_length = read_varint(sock)
            json_data = sock.recv(json_length).decode('utf-8')
            return json.loads(json_data)
    except Exception as e:
        print("Status ping failed:", e)
        return None

def send_status_response(sock):
    response = ping_server(start=False)
    if response is None:
        response = {
            "version": {"name": "1.21.5", "protocol": 770},
            "players": {"max": 1, "online": 0},
            "description": {"text": "§aServer Down, Connect to Start"},
        }
    payload = write_string(json.dumps(response))
    send_packet(sock, 0x00, payload)

def send_login_success(sock, username):
    payload = uuid.uuid3(uuid.NAMESPACE_DNS, f"OfflinePlayer:{username}").bytes + write_string(username) + write_varint(0)
    send_packet(sock, 0x02, payload)

def send_transfer(sock):
    data = write_string(get_instance_ip()) + write_varint(TARGET_PORT)
    send_packet(sock, 0x0B, data)


def keep_alive(sock):
    keep_alive_id = random.randrange(-2**63, 2**63)
    payload = struct.pack(">q", keep_alive_id)  # Pack as signed long
    send_packet(sock, 0x04, payload)
    print("Sent KeepAlive ID:", keep_alive_id)

    # Wait for client response
    try:
        read_varint(sock)
        read_varint(sock)
    except Exception as e:
        print("Error reading KeepAlive response:", e)




# --- Main Handler --- #

def handle_client(sock):
    with sock:
        try:
            packet_length = read_varint(sock)
            packet_id = read_varint(sock)
            if packet_id == 0x00:  # Handshake
                print("[>] Starting Handshake")
                protocol_version = read_varint(sock)
                server_address_length = read_varint(sock)
                server_address = sock.recv(server_address_length)
                port = struct.unpack(">H", sock.recv(2))[0]
                next_state = read_varint(sock)

                if next_state == 1:  # Status
                    print("[>] Status")
                    read_varint(sock)  # Status Request
                    send_status_response(sock)
                    read_varint(sock)  # Ping
                    ping_payload = sock.recv(8)
                    send_packet(sock, 0x01, ping_payload)
                elif next_state == 2:  # Login
                    print("[>] Logging in")
                    read_varint(sock)  # Length
                    login_packet_id = read_varint(sock)
                    name_length = read_varint(sock)
                    username = sock.recv(name_length).decode('utf-8')
                    print("Connecting user: ",username)
                    flush_socket(sock)
                    send_login_success(sock, username)
                    read_varint(sock)
                    read_varint(sock)
                    while ping_server() is None:
                        keep_alive(sock)
                        time.sleep(1)
                    time.sleep(1)
                    send_transfer(sock)
                    print("Cleaning up user: ", username)
                    time.sleep(5)
        except Exception as e:
            print(f"Error: {e}")


# --- Start server --- #

def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, PORT))
        s.listen()
        print(f"[+] Listening on {HOST}:{PORT}")
        while True:
            conn, addr = s.accept()
            print(f"[>] Connection from {addr}")
            thread = threading.Thread(target=handle_client, args=(conn,))
            thread.start()

if __name__ == "__main__":
    main()

