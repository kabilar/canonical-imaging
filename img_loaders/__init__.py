import ast
import numpy as np
import re
from datetime import datetime


def parse_scanimage_header(scan):
    header = {}
    for item in scan.header.split('\n'):
        key, value = item.split(' = ')
        header[key.replace('.', '_')] = value
    return header


def get_scanimage_acq_time(scan):
    header = parse_scanimage_header(scan)
    recording_time = datetime.strptime((header['epoch'][1:-1]).replace(',', ' '),
                                       '%Y %m %d %H %M %S.%f')
    return recording_time
