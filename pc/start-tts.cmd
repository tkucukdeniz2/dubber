@echo off
rem dubber-tts: VoxCPM2 on the RTX 4080 SUPER for the Mac's dubber, on 127.0.0.1:8191.
rem The mode service (gpu_mod.py) starts it in dubbing mode, when Qwen and ComfyUI have left the 4080 SUPER:
rem it needs about 6.2 GB there. Do not start it by hand.
set CUDA_DEVICE_ORDER=PCI_BUS_ID
set CUDA_VISIBLE_DEVICES=GPU-<uuid of the GPU for the voice, from nvidia-smi -L>
set HF_HOME=C:\AI\tts\hf
set HF_HUB_OFFLINE=1
set TORCHINDUCTOR_CACHE_DIR=C:\AI\tts\inductor-cache
set PYTHONIOENCODING=utf-8
"C:\AI\tts\venv\Scripts\python.exe" "C:\AI\tts\tts_server.py" >> "C:\AI\logs\tts.log" 2>&1
