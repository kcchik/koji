import os
import config
from piece import Piece
from math import ceil

class File():
    def __init__(self, file, offset):
        dirname = os.path.dirname(file['path'])
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        self.stream = open('./complete/%s' % file['path'], 'wb')
        self.offset = offset
        self.length = file['length']
        self.path = file['path']
        self.complete = False
