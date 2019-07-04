import threading
import socket
import hashlib
import struct
import sys
import time
import math
import bencode
import bitstring

import config
import cli
import factory


class Peer(threading.Thread):
    def __init__(self, address):
        threading.Thread.__init__(self)
        self.address = address
        self.socket = socket.socket()
        self.socket.settimeout(10)
        self.has = set()
        self.state = {
            'metadata_handshake': False,
            'handshake': False,
            'connected': True,
            'choking': True,
        }
        self.piece_index = -1
        self.metadata_id = -1


    def connect(self):
        try:
            self.socket.connect(self.address)
        except OSError as _:
            self.disconnect()


    def disconnect(self):
        if self.piece_index != -1:
            config.manager.pieces[self.piece_index]['requesting'] = False
        self.socket.close()
        self.state['connected'] = False
        sys.exit()


    def run(self):
        self.connect()
        self.send_handshake()
        self.parse_stream()


    def parse_stream(self):
        stream = b''
        while True:
            try:
                packet = self.socket.recv(4096)
            except OSError as _:
                self.disconnect()

            if not packet:
                self.disconnect()

            if not self.state['handshake']:
                packet = self.handle_handshake(packet)

            stream_complete = True
            stream += packet
            while len(stream) >= 4:
                length = struct.unpack('>I', stream[:4])[0]
                if length == 0 or len(stream) < length + 4:
                    self.send(bytes(4))
                    stream_complete = False
                    break
                message = stream[4:length + 4]
                self.handle(message)
                stream = stream[length + 4:]

            if stream_complete:
                if not config.PIECE_SIZE:
                    if self.metadata_id != -1:
                        self.send_metadata_request()
                elif self.state['choking']:
                    self.send_interested()
                else:
                    self.send_request()


    def handle(self, message):
        message_id = message[0]
        payload = message[1:] if len(message) > 1 else b''

        # Choke
        if message_id == 0:
            self.state['choking'] = True
        # Unchoke
        elif message_id == 1:
            self.state['choking'] = False
        # Have
        elif message_id == 4:
            self.handle_have(payload)
        # Bitfield
        elif message_id == 5:
            self.handle_bitfield(payload)
        # Block
        elif message_id == 7:
            self.handle_block(payload)
        # Metadata
        elif message_id == 20 and not config.PIECE_SIZE:
            # Handshake
            if not self.state['metadata_handshake']:
                self.handle_metadata_handshake(payload)
            # Piece
            else:
                self.handle_metadata_piece(payload)


    def handle_handshake(self, packet):
        pstrlen = packet[0]
        info_hash = struct.unpack('>20s', packet[pstrlen + 9:pstrlen + 29])[0]

        # Validate handshake
        if info_hash != config.tracker.info_hash:
            self.disconnect()

        self.state['handshake'] = True
        return packet[pstrlen + 49:]


    def handle_metadata_handshake(self, payload):
        metadata = dict(bencode.bdecode(payload[1:]).items())

        # Validate metadata
        if any(key not in metadata for key in ['m', 'metadata_size']):
            self.disconnect()
        m = dict(metadata['m'].items())
        if 'ut_metadata' not in m:
            self.disconnect()

        # Initialize metadata piece array
        if not config.manager.pieces:
            num_pieces = math.ceil(metadata['metadata_size'] / config.BLOCK_SIZE)
            config.manager.pieces = [factory.piece() for _ in range(num_pieces)]

        self.metadata_id = m['ut_metadata']
        self.state['metadata_handshake'] = True


    def handle_metadata_piece(self, payload):
        # Split into metadata and bytes
        i = payload.index(b'ee') + 2
        metadata = dict(bencode.bdecode(payload[1:i]).items())

        # Validate metadata
        if any(key not in metadata for key in ['msg_type', 'piece']):
            self.disconnect()

        self.piece_index = metadata['piece']
        piece = config.manager.pieces[self.piece_index]
        if metadata['msg_type'] == 1 and not piece['complete']:
            piece['value'] = payload[i:]
            piece['complete'] = True
            self.printf('\033[92m𝑖\033[0m', self.piece_index)
        self.piece_index = -1


    def handle_have(self, payload):
        i = struct.unpack('>I', payload)[0]
        self.has.add(i)


    def handle_bitfield(self, payload):
        bit_array = list(bitstring.BitArray(payload))
        self.has.update([i for i, available in enumerate(bit_array) if available])


    def handle_block(self, payload):
        # Split into metadata and bytes
        i, offset = struct.unpack('>II', payload[:8])
        block = payload[8:]

        # Validate metadata
        if self.piece_index != i:
            return

        piece = config.manager.pieces[i]
        piece['blocks'][offset // config.BLOCK_SIZE]['value'] = block
        blocks = [block['value'] for block in piece['blocks']]

        # Piece is complete
        if all(blocks):
            # Validate piece
            if hashlib.sha1(b''.join(blocks)).digest() == piece['value']:
                piece['complete'] = True
                self.printf('\033[92m✓\033[0m', i)
            else:
                piece['blocks'] = [factory.block() for _ in piece['blocks']]
                self.has.remove(i)
                self.printf('\033[91m✗\033[0m', i)

            # Reset peer
            self.piece_index = -1
            piece['requesting'] = False


    def send(self, message):
        try:
            self.socket.send(message)
        except OSError as _:
            return


    def send_handshake(self):
        pstr = b'BitTorrent protocol'
        pstrlen = bytes([len(pstr)])
        reserved = bytes(8) if config.COMMAND == 'torrent' else b'\x00\x00\x00\x00\x00\x10\x00\x00'
        message = (
            pstrlen
            + pstr
            + reserved
            + config.tracker.info_hash
            + config.tracker.peer_id
        )
        self.send(message)


    def send_metadata_request(self):
        for i, piece in enumerate(config.manager.pieces):
            if not piece['complete']:
                self.piece_index = i
                metadata = bencode.bencode({
                    'msg_type': 0,
                    'piece': self.piece_index,
                })
                message = struct.pack('>IBB', len(metadata) + 2, 20, self.metadata_id) + metadata
                self.send(message)
                break


    def send_interested(self):
        message = struct.pack('>IB', 1, 2)
        self.send(message)


    def send_request(self):
        # After connecting and completing a piece, peers will wait in this loop until another piece is available
        while self.piece_index == -1:
            # Look for available piece
            for i, piece in enumerate(config.manager.pieces):
                if not piece['requesting'] and not piece['complete'] and i in self.has:
                    piece['requesting'] = True
                    self.piece_index = i
                    break
            # No available pieces
            else:
                time.sleep(0.1)

            # Torrent is complete
            if all(file['complete'] for file in config.manager.files):
                self.disconnect()

        piece = config.manager.pieces[self.piece_index]
        block_size = config.BLOCK_SIZE
        blocks_left = sum(1 for block in piece['blocks'] if not block['value'])
        # Last block of the last piece
        if self.piece_index + 1 == len(config.manager.pieces) and blocks_left == 1:
            block_size = config.manager.length % config.BLOCK_SIZE
        offset = (len(piece['blocks']) - blocks_left) * config.BLOCK_SIZE
        message = struct.pack(
            '>IBIII',
            13,
            6,
            self.piece_index,
            offset,
            block_size
        )
        self.send(message)


    def printf(self, indicator, iteration):
        cli.printf('{} {}/{}'.format(indicator, iteration + 1, len(config.manager.pieces)), prefix=self.address[0])
