#!/usr/bin/env python3
from sticker_project import main
import sys
if __name__ == '__main__':
    raise SystemExit(main(['build', *sys.argv[1:]]))
