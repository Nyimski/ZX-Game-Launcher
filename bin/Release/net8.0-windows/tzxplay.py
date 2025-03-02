import sys
import os

# Add the embedded Python's site-packages folder to sys.path
python_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "python")
site_packages = os.path.join(python_dir, "Lib", "site-packages")
sys.path.append(site_packages)

# Now import the required modules
import argparse
import numpy
import sounddevice as sd
import struct
import time
import wave
import os

from tzxlib.tapfile import TapHeader
from tzxlib.tzxfile import TzxFile
from tzxlib.tzxblocks import TzxbLoopStart, TzxbLoopEnd, TzxbJumpTo, TzxbPause, TzxbStopTape48k
from tzxlib.saver import TapeSaver

wavelets = {}
numpySilence = numpy.zeros(1024, dtype=numpy.float32)

# Control file path
control_file_path = os.path.join(os.getenv("TEMP"), "tzx_control.txt")
# Current block file path
current_block_file = os.path.join(os.getenv("TEMP"), "current_block.txt")
# Total blocks file path
total_blocks_file = os.path.join(os.getenv("TEMP"), "total_blocks.txt")

def wavelet(length, level, sine=False, npy=True):
    type = (length, level)
    if type in wavelets:
        return wavelets[type]

    sign = 1 if level else -1

    amp = sign * (min(32767 * (length + 10) / 25, 32767) if sine else 32000) / 32767
    wave = numpy.empty(length, dtype=numpy.float32)
    for pos in range(length):
        wave[pos] = amp * numpy.sin(pos * numpy.pi / length) if sine else amp
    wavelets[type] = wave
    return wavelets[type]

def check_control_file():
    """Check the control file for pause/resume/stop/rewind commands."""
    if os.path.exists(control_file_path):
        with open(control_file_path, "r") as f:
            command = f.read().strip()
        os.remove(control_file_path)
        return command
    return None

def streamAudio(tzx: TzxFile, rate=44100, stopAlways=False, stop48k=False, sine=False, cpufreq=3500000, verbose=False):
    saver = TapeSaver(cpufreq)
    block = 0
    repeatBlock = None
    repeatCount = None
    currentSampleTime = 0
    realTimeNs = 0
    paused = False

    # Delete existing temporary files at the start of playback
    if os.path.exists(current_block_file):
        os.remove(current_block_file)
    if os.path.exists(total_blocks_file):
        os.remove(total_blocks_file)

    try:
        # Write the total number of blocks to the file (adjusted for zero-based indexing)
        total_blocks = len(tzx.blocks)
        with open(total_blocks_file, "w") as f:
            f.write(str(total_blocks - 1))  # Subtract 1 for zero-based indexing
    except Exception as e:
        print(f"Error writing total blocks: {e}")

    while block < len(tzx.blocks):
        try:
            # Write the current block index to the file
            with open(current_block_file, "w") as f:
                f.write(str(block))
        except Exception as e:
            print(f"Error writing current block: {e}")

        # Check for pause/resume/stop/rewind commands
        command = check_control_file()
        if command == "pause":
            paused = True
        elif command == "resume":
            paused = False
        elif command == "stop":
            break
        elif command and command.startswith("rewind"):
            # Extract the reset block index from the command
            try:
                resetBlock = int(command.split(":")[1])
                block = resetBlock
                currentSampleTime = 0
                realTimeNs = 0
                continue
            except (IndexError, ValueError):
                block = 0
                currentSampleTime = 0
                realTimeNs = 0
                continue

        if paused:
            time.sleep(0.1)  # Sleep briefly to avoid busy-waiting
            continue

        b = tzx.blocks[block]
        if verbose:
            millis = realTimeNs // 1000000
            seconds = millis // 1000
            minutes = seconds // 60
            print('%02d:%02d.%03d %3d %-30s %s' % (minutes, seconds % 60, millis % 1000, block, b.type, str(b)))
        block += 1

        if isinstance(b, TzxbLoopStart):
            repeatBlock = block
            repeatCount = b.repeats()
            continue
        elif isinstance(b, TzxbLoopEnd) and repeatBlock is not None:
            repeatCount -= 1
            if repeatCount > 0:
                block = repeatBlock
                continue
            else:
                repeatBlock = None
                repeatCount = None
                continue
        elif isinstance(b, TzxbJumpTo):
            block += b.relative() - 1
            if block < 0 or block > len(tzx.blocks) - 1:
                raise IndexError('Jump to non-existing block')
            continue
        elif isinstance(b, TzxbPause) and b.stopTheTape() and stopAlways:
            break
        elif isinstance(b, TzxbStopTape48k) and stop48k:
            break

        currentLevel = False
        lastLevel = False
        for ns in b.playback(saver):
            # Check for pause/resume/stop/rewind commands during playback
            command = check_control_file()
            if command == "pause":
                paused = True
            elif command == "resume":
                paused = False
            elif command == "stop":
                return  # Exit the function to stop playback
            elif command and command.startswith("rewind"):
                try:
                    resetBlock = int(command.split(":")[1])
                    block = resetBlock
                    currentSampleTime = 0
                    realTimeNs = 0
                    break
                except (IndexError, ValueError):
                    block = 0
                    currentSampleTime = 0
                    realTimeNs = 0
                    break

            if paused:
                time.sleep(0.1)  # Sleep briefly to avoid busy-waiting
                continue

            currentLevel = not currentLevel
            if ns > 0:
                realTimeNs += ns
                newSampleTime = ((realTimeNs * rate) + 500000000) // 1000000000
                wavelen = newSampleTime - currentSampleTime
                if wavelen <= 0:
                    continue
                if currentLevel != lastLevel:
                    yield wavelet(wavelen, currentLevel, sine)
                else:
                    while wavelen > 0:
                        if wavelen >= len(numpySilence):
                            yield numpySilence
                            wavelen -= len(numpySilence)
                        else:
                            yield numpy.zeros(wavelen, dtype=numpy.float32)
                            wavelen = 0
                lastLevel = currentLevel
                currentSampleTime = newSampleTime
    print("Playback finished successfully.")

def main():
    parser = argparse.ArgumentParser(description='Playback a tzx file with pause functionality')
    parser.add_argument('file', nargs='?', type=argparse.FileType('rb'), help='TZX file')
    args = parser.parse_args()

    if args.file is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    tzx = TzxFile()
    tzx.read(args.file)

    stream = streamAudio(tzx)

    try:
        with sd.Stream(samplerate=44100, channels=1, latency='high') as out:
            for b in stream:
                if b is None:
                    break
                out.write(b)
    except KeyboardInterrupt:
        print("\nPlayback stopped.")
    except Exception as e:
        print(f"\nAn error occurred: {e}")
    finally:
        print("Playback finished successfully.")

if __name__ == "__main__":
    main()
