#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gpu-mod: qwen-pc'de RTX 4080 SUPER'i yerel LLM (llama.cpp) ile video (ComfyUI) arasında paylaştırır.

Üç mod vardır; ekran kartı belleği hepsine aynı anda yetmez:
  llm    : Qwen/Gemma sunucusu (Qwen38-LlamaServer görevi) iki ekran kartında çalışır, ComfyUI kapalı.
  video  : ComfyUI RTX 4080 SUPER'de çalışır. "hibrit" açıksa Gemma da RTX 4070'te çalışmaya devam eder,
           böylece video üretilirken `qclaude -g` kullanılabilir.
  dublaj : Mac'teki dubber için: ses servisi (dubber-tts, VoxCPM2) RTX 4080 SUPER'de, çevirmen
           (models-dublaj.ini, ör. TranslateGemma) RTX 4070'te, 8080'de. Qwen ve ComfyUI kapalı.

HTTP (127.0.0.1:8190; Tailscale serve ile yalnızca tailnet'e açılır). Anahtar: LLM sunucusunun API anahtarı.
  GET  /saglik           anahtarsız; "ok"
  GET  /durum            mod, kuyruk, boşta geçen süre
  POST /video            video moduna geç (LLM durur, ComfyUI başlar)
  POST /dublaj[?zorla=1] dublaj moduna geç (LLM ve ComfyUI durur; ses servisi ve 4070 çevirmeni başlar)
  POST /llm[?zorla=1]    LLM moduna geç (ComfyUI'de iş varsa zorla=1 olmadan reddedilir)
  GET  /modeller         kurulu ComfyUI model dosyaları (her iki modda da)
  GET  /video-bilgi?ad=  input klasöründeki bir videonun süresi, fps'i, boyutu (parça planı için)
  GET  /parcalar?onek=   qvideo'nun bitmiş parçaları (yarıda kalan işe devam için)
  POST /parca-sil        qvideo'nun birleştirilmiş ara parçalarını siler (yalnızca output\\qvideo\\parca)
Video modunda ComfyUI, dublaj modunda ses servisi belirli bir süre boşta kalırsa (varsayılan 45 dk)
kendiliğinden LLM moduna dönülür. LLM ya da video moduna geçerken ses servisi her zaman kapatılır.
Dublaj modunda ses servisi kapanır ya da üst üste başarısız olduğunu bildirirse (/health 503 "failed")
LLM moduna dönülür; yalnızca çevirmen kapanırsa o yeniden başlatılır. /dublaj?zorla=1 zaten dublaj
modundayken de iki servisi yeniden başlatır.
Ayarlar: C:\\AI\\config\\gpu-mod.json
"""
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get('GPU_MOD_CONFIG', r'C:\AI\config\gpu-mod.json')
DEFAULTS = {
    'listen': '127.0.0.1',
    'port': 8190,
    'key_file': r'C:\AI\config\api-keys.txt',
    'llm_task': 'Qwen38-LlamaServer',
    'comfy_task': 'Qwen38-ComfyUI',
    'llm_url': 'http://127.0.0.1:8080',
    'comfy_url': 'http://127.0.0.1:8188',
    'comfy_public_url': '',
    'comfy_main': r'C:\AI\comfyui\ComfyUI\main.py',
    'comfy_base': r'C:\AI\comfyui',
    'comfy_pid_file': r'C:\AI\comfyui\logs\comfyui.pid',
    'comfy_log': r'C:\AI\comfyui\logs\comfyui.log',
    'idle_minutes': 45,
    'hybrid': True,
    'hybrid_preset': r'C:\AI\config\models-video.ini',
    'hybrid_cuda_visible': '',          # 4070'in UUID'i: hibrit Gemma yalnızca bu kartı görür
    'server_config': r'C:\AI\config\server-config.json',
    'log': r'C:\AI\logs\gpu-mod.log',
    'hybrid_log': r'C:\AI\logs\server-video.log',
    'tts_cmd': r'C:\AI\tts\start-tts.cmd',            # dubber-tts'i 4080 SUPER'e sabitleyip başlatır
    'tts_main': r'C:\AI\tts\tts_server.py',
    'tts_url': 'http://127.0.0.1:8191',
    'dub_preset': r'C:\AI\config\models-dublaj.ini',   # dublaj modunda 4070'teki çevirmen
    'dub_log': r'C:\AI\logs\server-dublaj.log',
    'dub_idle_minutes': 45,
}
FAKE = os.environ.get('GPU_MOD_FAKE')   # testler için: sistem komutları yerine bu klasördeki dosyalar
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding='utf-8-sig') as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


CFG = load_config()


def log(msg):
    line = '%s  %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        with open(CFG['log'], 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    if FAKE:
        sys.stderr.write(line + '\n')


def read_key():
    with open(CFG['key_file'], encoding='utf-8-sig') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                return line
    raise RuntimeError('API anahtarı yok: ' + CFG['key_file'])


# ----------------------------------------------------------------------------- sistem işlemleri
def run(cmd, timeout=60, stdout_only=False):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        out = p.stdout if stdout_only else p.stdout + p.stderr
        return p.returncode, out.decode('utf-8', 'replace')
    except Exception as e:  # zaman aşımı vb.
        return -1, str(e)


def ps(script, timeout=60):
    """PowerShell komutu; yalnızca stdout (ilerleme/CLIXML çıktısı karışmasın)."""
    return run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                "$ProgressPreference='SilentlyContinue'; " + script], timeout=timeout, stdout_only=True)


def http_json(url, method='GET', body=None, timeout=5, headers=None):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict({'Content-Type': 'application/json'}, **(headers or {})))
    with OPENER.open(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode('utf-8')) if raw.strip() else {}


def http_ok(url, timeout=3):
    try:
        with OPENER.open(urllib.request.Request(url), timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


class System:
    """Windows'ta görevleri ve süreçleri yönetir. GPU_MOD_FAKE ile testte dosyalarla taklit edilir."""

    def _f(self, name):
        return os.path.join(FAKE, name)

    # --- LLM (llama.cpp router, Qwen38-LlamaServer görevi)
    def llm_up(self):
        if FAKE:   # 8080'i 4070'teki hibrit/dublaj sunucusu da yanıtlar
            return os.path.exists(self._f('llm.on')) or os.path.exists(self._f('hybrid.on'))
        return http_ok(CFG['llm_url'] + '/health')

    def llama_procs(self):
        if FAKE:
            return os.path.exists(self._f('llm.on')) or os.path.exists(self._f('hybrid.on'))
        rc, out = run(['tasklist', '/FI', 'IMAGENAME eq llama-server.exe', '/NH'])
        return 'llama-server.exe' in out.lower()

    def stop_llm(self):
        if FAKE:
            for n in ('llm.on', 'hybrid.on'):
                if os.path.exists(self._f(n)):
                    os.remove(self._f(n))
            return
        run(['schtasks', '/End', '/TN', CFG['llm_task']])
        run(['taskkill', '/F', '/T', '/IM', 'llama-server.exe'])
        for _ in range(30):
            if not self.llama_procs():
                return
            time.sleep(1)
            run(['taskkill', '/F', '/T', '/IM', 'llama-server.exe'])

    def llm_task_running(self):
        if FAKE:
            return os.path.exists(self._f('llm.on'))
        rc, out = ps("(Get-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue).State" % CFG['llm_task'].replace("'", "''"))
        return out.strip().lower().endswith('running')

    def start_llm(self):
        if FAKE:
            open(self._f('llm.on'), 'w').close()
            return
        run(['schtasks', '/Run', '/TN', CFG['llm_task']])

    # --- Video modunda 4070'te Gemma (hibrit); dublaj modunda 4070'te çevirmen (aynı yol, başka ön ayar)
    def start_hybrid(self, preset=None, log_path=None):
        preset, log_path = preset or CFG['hybrid_preset'], log_path or CFG['hybrid_log']
        if FAKE:
            if os.path.exists(self._f('hybrid.fail')):
                return False
            open(self._f('hybrid.on'), 'w').write(preset)
            return True
        try:
            with open(CFG['server_config'], encoding='utf-8-sig') as f:
                sc = json.load(f)
            args = list(sc['Args'])
            if '--models-preset' not in args:
                return False
            args[args.index('--models-preset') + 1] = preset
            env = dict(os.environ)
            if CFG.get('hybrid_cuda_visible'):
                env['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
                env['CUDA_VISIBLE_DEVICES'] = CFG['hybrid_cuda_visible']
            with open(log_path, 'ab') as logf:
                subprocess.Popen([sc['ServerExe']] + args, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, env=env,
                                 creationflags=NO_WINDOW | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
            return True
        except Exception as e:
            log('Hibrit LLM başlatılamadı: %s' % e)
            return False

    # --- Ses servisi (dubber-tts, dublaj modu): start-tts.cmd onu 4080 SUPER'e sabitler
    def tts_up(self):
        if FAKE:
            return os.path.exists(self._f('tts.on'))
        return http_ok(CFG['tts_url'] + '/health')

    def tts_failed(self):
        """Ses servisi modeli yükleyemediğini ya da cümlelerin üst üste başarısız olduğunu bildiriyor mu."""
        if FAKE:
            return os.path.exists(self._f('tts.failed'))
        try:
            with OPENER.open(urllib.request.Request(CFG['tts_url'] + '/health'), timeout=3):
                return False
        except urllib.error.HTTPError as e:
            try:
                return e.code == 503 and e.read().strip() == b'failed'
            except Exception:
                return False
        except Exception:
            return False

    def tts_alive(self):
        """Ses servisinin süreci çalışıyor mu (model yüklenirken /health henüz yanıt vermeyebilir).

        Yalnızca kesin bir 0 "süreç yok" demektir: PowerShell'in zaman aşımı ya da beklenmedik bir
        çıktısı "var" sayılır, böylece sağlıklı bir açılış yanlışlıkla kesilmez.
        """
        if FAKE:
            return os.path.exists(self._f('tts.on')) or os.path.exists(self._f('tts.failed'))
        rc, out = ps("@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
                     "$_.CommandLine -like '*%s*' }).Count" % os.path.basename(CFG['tts_main']).replace("'", "''"))
        return not (rc == 0 and out.strip() == '0')

    def tts_idle_s(self):
        """Son seslendirmeden bu yana geçen saniye; ses servisine ulaşılamazsa None."""
        if FAKE:
            try:
                return float(open(self._f('tts.idle')).read().strip() or 0)
            except (OSError, ValueError):
                return 0.0
        try:
            st = http_json(CFG['tts_url'] + '/status', headers={'Authorization': 'Bearer ' + (KEY or '')})
            return float(st['idle_s'])
        except Exception:
            return None

    def start_tts(self):
        if FAKE:
            open(self._f('tts.on'), 'w').close()
            return
        subprocess.Popen(['cmd.exe', '/c', CFG['tts_cmd']], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=NO_WINDOW | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))

    def stop_tts(self):
        if FAKE:
            if os.path.exists(self._f('tts.on')):
                os.remove(self._f('tts.on'))
            return
        # komut satırında tts_server.py geçen python süreçleri (venv'in python.exe'si ve asıl yorumlayıcı)
        ps("Get-CimInstance Win32_Process | "
           "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*%s*' } | "
           "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
           % os.path.basename(CFG['tts_main']).replace("'", "''"))
        for _ in range(20):
            if not self.tts_alive():
                return
            time.sleep(1)
        log('Ses servisi 20 sn içinde kapanmadı; 4080 SUPER hâlâ dolu olabilir')

    # --- ComfyUI (Qwen38-ComfyUI görevi)
    def comfy_up(self):
        if FAKE:
            return os.path.exists(self._f('comfy.on'))
        return http_ok(CFG['comfy_url'] + '/system_stats')

    def comfy_queue(self):
        """Çalışan + bekleyen iş sayısı; ComfyUI kapalıysa None."""
        if FAKE:
            if not self.comfy_up():
                return None
            try:
                return int(open(self._f('queue')).read().strip() or 0)
            except (OSError, ValueError):
                return 0
        try:
            q = http_json(CFG['comfy_url'] + '/queue')
            return len(q.get('queue_running') or []) + len(q.get('queue_pending') or [])
        except Exception:
            return None

    def comfy_last_job(self):
        if FAKE:
            try:
                return open(self._f('history')).read().strip()
            except OSError:
                return ''
        try:
            h = http_json(CFG['comfy_url'] + '/history?max_items=1')
            return ','.join(sorted(h.keys()))
        except Exception:
            return ''

    def comfy_cancel_all(self):
        if FAKE:
            open(self._f('queue'), 'w').write('0')
            return
        for path, body in (('/queue', {'clear': True}), ('/interrupt', {})):
            try:
                http_json(CFG['comfy_url'] + path, 'POST', body)
            except Exception:
                pass

    def start_comfy(self):
        if FAKE:
            open(self._f('comfy.on'), 'w').close()
            return
        run(['schtasks', '/Run', '/TN', CFG['comfy_task']])

    def stop_comfy(self):
        if FAKE:
            if os.path.exists(self._f('comfy.on')):
                os.remove(self._f('comfy.on'))
            return
        run(['schtasks', '/End', '/TN', CFG['comfy_task']])
        main = CFG['comfy_main'].lower()
        try:
            with open(CFG['comfy_pid_file']) as f:
                pid = f.read().strip()
            # PID yeniden kullanılmış olabilir: yalnızca gerçekten ComfyUI ise süreç ağacını kapat
            if pid.isdigit():
                rc, cl = ps("(Get-CimInstance Win32_Process -Filter 'ProcessId=%s').CommandLine" % pid)
                if main in cl.lower():
                    run(['taskkill', '/F', '/T', '/PID', pid])
        except OSError:
            pass
        try:
            os.remove(CFG['comfy_pid_file'])
        except OSError:
            pass
        # yedek: komut satırında ComfyUI main.py geçen python süreçleri
        ps("Get-CimInstance Win32_Process | "
           "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*%s*' } | "
           "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" % CFG['comfy_main'].replace("'", "''"))


SYS = System()


# ----------------------------------------------------------------------------- mod yönetimi
class Modes:
    def __init__(self):
        self.lock = threading.Lock()
        self.transition = None
        self.last_active = time.time()
        self.last_job = ''
        self.hybrid_on = False
        self.dub_llm_on = False
        self.mode = 'video' if SYS.comfy_up() else 'dublaj' if SYS.tts_up() else 'llm'
        self.dub_llm_on = self.mode == 'dublaj' and SYS.llm_up()
        log('gpu-mod başladı; mod: %s' % self.mode)

    def status(self):
        comfy = SYS.comfy_up()
        q = SYS.comfy_queue() if comfy else None
        idle = int((time.time() - self.last_active) / 60) if self.mode == 'video' else 0
        dub = self.mode == 'dublaj'
        tts = SYS.tts_up()
        tts_idle = SYS.tts_idle_s() if dub and tts else None
        return {
            'mod': self.transition and 'gecis' or self.mode,
            'gecis_hedefi': self.transition,
            'llm': {'calisiyor': SYS.llm_up(), 'hibrit_gemma_4070': self.mode == 'video' and self.hybrid_on,
                    'dublaj_cevirmen_4070': dub and self.dub_llm_on},
            'comfyui': {'calisiyor': comfy, 'kuyruk': q, 'adres': CFG.get('comfy_public_url') or CFG['comfy_url']},
            'dublaj': {'ses_calisiyor': tts, 'bosta_dk': int(tts_idle / 60) if tts_idle is not None else None,
                       'otomatik_llm_donusu_dk': CFG['dub_idle_minutes']},
            'bosta_dk': idle,
            'otomatik_llm_donusu_dk': CFG['idle_minutes'],
        }

    def to_video(self):
        with self.lock:
            if self.mode == 'video' and SYS.comfy_up():
                return 200, {'tamam': True, 'mesaj': 'Zaten video modunda.'}
            self.transition = 'video'
            try:
                log('Video moduna geçiliyor: LLM durduruluyor')
                SYS.stop_llm()
                SYS.stop_tts()          # dublaj modundan gelindiyse ses servisi 4080 SUPER'i boşaltsın
                self.dub_llm_on = False
                time.sleep(2)
                log('ComfyUI başlatılıyor')
                SYS.start_comfy()
                t0 = time.time()
                while not SYS.comfy_up():
                    if time.time() - t0 > 600:
                        log('ComfyUI 10 dk içinde açılmadı; LLM geri başlatılıyor')
                        SYS.stop_comfy()
                        SYS.start_llm()
                        self.mode = 'llm'
                        return 500, {'tamam': False, 'mesaj': 'ComfyUI açılmadı; LLM moduna geri dönüldü. Günlük: ' + CFG['comfy_log']}
                    time.sleep(2)
                self.hybrid_on = bool(CFG.get('hybrid')) and os.path.exists(CFG['hybrid_preset']) and SYS.start_hybrid()
                self.mode = 'video'
                self.last_active = time.time()
                self.last_job = SYS.comfy_last_job()
                log('Video modu hazır (%d sn; 4070\'te Gemma: %s)' % (time.time() - t0, 'evet' if self.hybrid_on else 'hayır'))
                return 200, {'tamam': True, 'mesaj': 'Video modu hazır.', 'hibrit_gemma_4070': self.hybrid_on}
            finally:
                self.transition = None

    def to_llm(self, force=False, reason='istek', only_if=None):
        with self.lock:
            if only_if is not None and not only_if():
                return 200, {'tamam': True, 'mesaj': 'LLM moduna dönmeye gerek kalmadı.'}
            if self.mode == 'llm' and SYS.llm_up() and not SYS.comfy_up() and not SYS.tts_up():
                return 200, {'tamam': True, 'mesaj': 'Zaten LLM modunda.'}
            q = SYS.comfy_queue()
            if q and not force:
                return 409, {'tamam': False, 'mesaj': 'ComfyUI\'de %d iş var (çalışan/bekleyen). Bitmesini bekleyin ya da zorlayın (Mac: pc-llm --zorla).' % q, 'kuyruk': q}
            self.transition = 'llm'
            try:
                log('LLM moduna geçiliyor (%s)' % reason)
                if q:
                    SYS.comfy_cancel_all()
                SYS.stop_llm()          # video modundaki 4070 Gemma'sı ya da dublaj modundaki çevirmen
                self.hybrid_on = False
                self.dub_llm_on = False
                SYS.stop_tts()          # Qwen 4080 SUPER'e sığsın diye ses servisi önce kapanır
                SYS.stop_comfy()
                for _ in range(30):
                    if not SYS.comfy_up():
                        break
                    time.sleep(1)
                time.sleep(2)
                SYS.start_llm()
                t0 = time.time()
                while not SYS.llm_up():
                    if time.time() - t0 > 300:
                        log('LLM sunucusu 5 dk içinde yanıt vermedi')
                        self.mode = 'llm'
                        return 500, {'tamam': False, 'mesaj': 'LLM sunucusu 5 dk içinde açılmadı. Günlük: C:\\AI\\logs\\server.log'}
                    time.sleep(2)
                self.mode = 'llm'
                log('LLM modu hazır (%d sn)' % (time.time() - t0))
                return 200, {'tamam': True, 'mesaj': 'LLM modu hazır (model ilk istekte/açılışta yüklenir).'}
            finally:
                self.transition = None

    def to_dub(self, force=False):
        with self.lock:
            if self.mode == 'dublaj' and SYS.tts_up() and SYS.llm_up() and not force:
                return 200, {'tamam': True, 'mesaj': 'Zaten dublaj modunda.'}
            if not os.path.exists(CFG['dub_preset']):
                return 500, {'tamam': False, 'mesaj': 'Dublaj çevirmeni ön ayarı yok: ' + CFG['dub_preset']}
            q = SYS.comfy_queue()
            if q and not force:
                return 409, {'tamam': False, 'mesaj': 'ComfyUI\'de %d iş var (çalışan/bekleyen). Bitmesini bekleyin ya da zorlayın (Mac: pc-dublaj --zorla).' % q, 'kuyruk': q}
            self.transition = 'dublaj'
            try:
                log('Dublaj moduna geçiliyor')
                if q:
                    SYS.comfy_cancel_all()
                SYS.stop_comfy()
                SYS.stop_llm()
                self.hybrid_on = False
                SYS.stop_tts()
                time.sleep(2)
                self.dub_llm_on = SYS.start_hybrid(CFG['dub_preset'], CFG['dub_log'])
                if self.dub_llm_on:
                    SYS.start_tts()
                t0 = time.time()
                next_proc_check = 30   # süreç listesi pahalı (Get-CimInstance): 30. sn'den sonra 10 sn'de bir
                while not (SYS.tts_up() and SYS.llm_up()):
                    waited = time.time() - t0
                    why = None
                    if not self.dub_llm_on:
                        why = 'çevirmen başlamadı'
                    elif waited > 15 and not SYS.llama_procs():
                        why = 'çevirmen kapandı'   # ör. bozuk models-dublaj.ini: llama-server hemen çıkar
                    elif SYS.tts_failed():
                        why = 'ses servisi başarısız'
                    elif waited > next_proc_check:
                        next_proc_check = waited + 10
                        if not SYS.tts_alive():
                            why = 'ses servisi süreci yok'
                    if why is None and waited > 600:
                        why = '10 dk içinde hazır olmadı'
                    if why:
                        log('Dublaj servisleri açılmadı (%s); LLM moduna geri dönülüyor' % why)
                        SYS.stop_tts()
                        SYS.stop_llm()
                        self.dub_llm_on = False
                        time.sleep(2)
                        SYS.start_llm()
                        self.mode = 'llm'
                        return 500, {'tamam': False, 'mesaj': 'Dublaj servisleri açılmadı (%s); LLM moduna geri dönüldü. Günlükler: C:\\AI\\logs\\tts.log, %s' % (why, CFG['dub_log'])}
                    time.sleep(2)
                self.mode = 'dublaj'
                log('Dublaj modu hazır (%d sn)' % (time.time() - t0))
                return 200, {'tamam': True, 'mesaj': 'Dublaj modu hazır.'}
            finally:
                self.transition = None

    def watch_dub(self):
        """Dublaj modu: ses servisi kapanırsa ya da uzun süre boşta kalırsa LLM moduna döner."""
        if not SYS.tts_up():
            self.down_count = getattr(self, 'down_count', 0) + 1
            if self.down_count >= 3:
                log('Ses servisi kapanmış ya da başarısız (çökmüş olabilir); LLM moduna dönülüyor')
                self.down_count = 0
                self.to_llm(force=True, reason='ses servisi kapandı',
                            only_if=lambda: self.mode == 'dublaj' and not SYS.tts_up())
            return
        self.down_count = 0
        if not SYS.llm_up():
            self.llm_down_count = getattr(self, 'llm_down_count', 0) + 1
            if self.llm_down_count >= 3:
                log('Dublaj çevirmeni kapanmış; yeniden başlatılıyor')
                self.llm_down_count = 0
                with self.lock:
                    if self.mode == 'dublaj' and not SYS.llm_up():
                        SYS.stop_llm()
                        self.dub_llm_on = SYS.start_hybrid(CFG['dub_preset'], CFG['dub_log'])
        else:
            self.llm_down_count = 0
        if SYS.llm_task_running():
            # dublaj modundayken Qwen görevi bir şekilde yeniden başladıysa 4070'i çevirmene geri ver
            with self.lock:
                # kilit beklenirken LLM moduna dönülmüş olabilir: o zaman Qwen'in çalışması doğrudur
                if self.mode == 'dublaj' and SYS.llm_task_running():
                    log('Dublaj modunda LLM görevi çalışıyor bulundu; durduruluyor')
                    SYS.stop_llm()
                    self.dub_llm_on = SYS.start_hybrid(CFG['dub_preset'], CFG['dub_log'])
        idle = SYS.tts_idle_s()
        idle_min = float(CFG.get('dub_idle_minutes') or 0)
        if idle is not None and idle_min > 0 and idle > idle_min * 60:
            log('Dublaj %.0f dk boşta kaldı; LLM moduna dönülüyor' % idle_min)
            self.to_llm(force=True, reason='dublaj boşta kaldı',
                        only_if=lambda: self.mode == 'dublaj' and (SYS.tts_idle_s() or 0) > idle_min * 60)

    def watch(self):
        """Video modunda ComfyUI, dublaj modunda ses servisi uzun süre boşta kalırsa LLM moduna döner."""
        while True:
            time.sleep(float(os.environ.get('GPU_MOD_WATCH_SEC', '60')))
            try:
                if self.transition:
                    self.down_count = 0
                    continue
                if self.mode == 'dublaj':
                    self.watch_dub()
                    continue
                if self.mode != 'video':
                    self.down_count = 0
                    continue
                if not SYS.comfy_up():
                    self.down_count = getattr(self, 'down_count', 0) + 1
                    if self.down_count >= 3:
                        log('ComfyUI kapanmış (çökmüş olabilir); LLM moduna dönülüyor')
                        self.down_count = 0
                        self.to_llm(force=True, reason='ComfyUI kapandı')
                    continue
                self.down_count = 0
                if SYS.llm_task_running():
                    # video modundayken LLM görevi bir şekilde yeniden başladıysa (ör. elle) ekran kartını geri al
                    log('Video modunda LLM görevi çalışıyor bulundu; durduruluyor')
                    with self.lock:
                        SYS.stop_llm()
                        self.hybrid_on = bool(CFG.get('hybrid')) and os.path.exists(CFG['hybrid_preset']) and SYS.start_hybrid()
                q = SYS.comfy_queue()
                job = SYS.comfy_last_job()
                if q or job != self.last_job:
                    self.last_active = time.time()
                    self.last_job = job
                idle_min = float(CFG.get('idle_minutes') or 0)
                if idle_min > 0 and time.time() - self.last_active > idle_min * 60:
                    log('ComfyUI %.0f dk boşta kaldı; LLM moduna dönülüyor' % idle_min)
                    self.to_llm(force=False, reason='boşta kaldı')
            except Exception as e:
                log('İzleme hatası: %s' % e)


MODES = None
KEY = None
MODEL_KLASORLERI = ('diffusion_models', 'unet', 'vae', 'text_encoders', 'clip', 'loras', 'upscale_models',
                    'frame_interpolation', 'latent_upscale_models', 'checkpoints')
ONEK_RE = re.compile(r'^qvideo/parca/[0-9a-f]{10}_$')
AD_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$')


def modeller():
    kok = os.path.join(CFG['comfy_base'], 'models')
    out = {}
    for k in MODEL_KLASORLERI:
        d = os.path.join(kok, k)
        try:
            out[k] = sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)) and not f.endswith('.indiriliyor'))
        except OSError:
            out[k] = []
    return {'tamam': True, 'klasorler': out}


def video_bilgi(ad):
    if not AD_RE.match(ad or '') or '..' in ad:
        return 400, {'tamam': False, 'mesaj': 'Geçersiz dosya adı'}
    yol = os.path.join(CFG['comfy_base'], 'input', ad)
    if not os.path.isfile(yol):
        return 404, {'tamam': False, 'mesaj': 'Dosya yok'}
    try:
        import av   # ComfyUI ortamındaki PyAV
    except ImportError:
        return 501, {'tamam': False, 'mesaj': 'PyAV yok'}
    try:
        with av.open(yol) as c:
            vs = next((x for x in c.streams if x.type == 'video'), None)
            if vs is None:
                return 422, {'tamam': False, 'mesaj': 'Dosyada video yok'}
            fps = float(vs.average_rate or vs.guessed_rate or vs.base_rate or 0) or 30.0
            sure = 0.0
            if vs.duration and vs.time_base:
                sure = float(vs.duration * vs.time_base)
            if not sure and c.duration:
                sure = c.duration / 1e6
            kare = int(vs.frames or 0) or int(round(sure * fps))
            return 200, {'tamam': True, 'genislik': vs.codec_context.width, 'yukseklik': vs.codec_context.height,
                         'fps': round(fps, 4), 'sure': round(sure, 3), 'kare': kare,
                         'ses': any(x.type == 'audio' for x in c.streams), 'kodek': vs.codec_context.name,
                         'boyut': os.path.getsize(yol)}
    except Exception as e:
        return 422, {'tamam': False, 'mesaj': 'Video okunamadı: %s' % e}


def parca_dosyalari(onek):
    if not ONEK_RE.match(onek or ''):
        return None, []
    d = os.path.join(CFG['comfy_base'], 'output', 'qvideo', 'parca')
    bas = onek.rsplit('/', 1)[1]
    try:
        return d, sorted(f for f in os.listdir(d) if f.startswith(bas) and f.lower().endswith(('.mp4', '.mkv', '.webm')))
    except OSError:
        return d, []


class Handler(BaseHTTPRequestHandler):
    server_version = 'gpu-mod/1.0'

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        body = (json.dumps(obj, ensure_ascii=False, indent=1) + '\n').encode('utf-8') if not isinstance(obj, str) else obj.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8' if not isinstance(obj, str) else 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        got = self.headers.get('x-api-key') or ''
        auth = self.headers.get('Authorization') or ''
        if auth.lower().startswith('bearer '):
            got = auth[7:].strip()
        return bool(got) and hmac.compare_digest(got.encode('utf-8'), KEY.encode('utf-8'))

    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/')
        if path == '/saglik':
            return self._send(200, 'ok')
        if not self._authorized():
            return self._send(401, {'tamam': False, 'mesaj': 'API anahtarı gerekli'})
        q = dict(urllib.parse.parse_qsl(self.path.partition('?')[2]))
        if path in ('', '/durum'):
            return self._send(200, MODES.status())
        if path == '/modeller':
            return self._send(200, modeller())
        if path == '/video-bilgi':
            return self._send(*video_bilgi(q.get('ad', '')))
        if path == '/parcalar':
            d, files = parca_dosyalari(q.get('onek', ''))
            if d is None:
                return self._send(400, {'tamam': False, 'mesaj': 'Geçersiz önek'})
            return self._send(200, {'tamam': True, 'dosyalar': files})
        self._send(404, {'tamam': False, 'mesaj': 'Bilinmeyen adres'})

    def do_POST(self):
        path, _, query = self.path.partition('?')
        path = path.rstrip('/')
        if not self._authorized():
            return self._send(401, {'tamam': False, 'mesaj': 'API anahtarı gerekli'})
        n = int(self.headers.get('Content-Length') or 0)
        body = {}
        if n:
            try:
                body = json.loads(self.rfile.read(n).decode('utf-8') or '{}')
            except ValueError:
                body = {}
        if path == '/parca-sil':
            d, files = parca_dosyalari(body.get('onek', ''))
            if d is None:
                return self._send(400, {'tamam': False, 'mesaj': 'Geçersiz önek'})
            silinen = 0
            for f in files:
                try:
                    os.remove(os.path.join(d, f))
                    silinen += 1
                except OSError:
                    pass
            log('qvideo ara parçaları silindi: %s (%d dosya)' % (body.get('onek'), silinen))
            return self._send(200, {'tamam': True, 'silinen': silinen})
        if path == '/video':
            code, res = MODES.to_video()
        elif path == '/llm':
            force = any(p in ('zorla=1', 'zorla=true', 'force=1') for p in query.split('&'))
            code, res = MODES.to_llm(force=force)
        elif path == '/dublaj':
            force = any(p in ('zorla=1', 'zorla=true', 'force=1') for p in query.split('&'))
            code, res = MODES.to_dub(force=force)
        else:
            code, res = 404, {'tamam': False, 'mesaj': 'Bilinmeyen adres'}
        res['durum'] = MODES.status()
        self._send(code, res)


def main():
    global MODES, KEY
    KEY = read_key()
    MODES = Modes()
    threading.Thread(target=MODES.watch, daemon=True).start()
    ThreadingHTTPServer.allow_reuse_address = False   # Windows: ikinci kopya aynı portu paylaşamasın
    srv = ThreadingHTTPServer((CFG['listen'], int(CFG['port'])), Handler)
    log('Dinleniyor: http://%s:%s' % (CFG['listen'], CFG['port']))
    srv.serve_forever()


if __name__ == '__main__':
    main()
