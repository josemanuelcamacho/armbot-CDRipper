#!/bin/bash

sudo apt update
sudo apt install abcde cdparanoia flac eject cd-discid
#sudo apt install libsnd1-dev libcdio-dev libcdio++-dev
python3 -m pip install --user arver
export PATH="$HOME/.local/bin:$PATH"
#./main.py --device /dev/sr0 --output "$HOME/Music" --drive-offset 102
