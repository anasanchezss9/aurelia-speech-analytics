"""Transforma os roteiros em ligações de telefone sintéticas.

Cada ligação sai como WAV G.711 μ-law, 8 kHz, estéreo, o mesmo formato das
gravações reais da central: o agente no canal esquerdo e a consultora no
direito. A voz é do Kokoro (Apache-2.0), que roda local e tem vozes em
português do Brasil. Para não soar como estúdio, cada ligação recebe:

- duas vozes diferentes, com variação de tom e velocidade por pessoa;
- pausas irregulares e sobreposição onde o roteiro marca interrupção;
- ambiente do lado da consultora (casa, rua, carro ou silêncio), murmúrio de
  central do lado do agente e quedas de sinal de celular;
- banda de telefone (300 a 3.400 Hz) e codificação μ-law em 8 kHz pelo ffmpeg.

Tudo é sorteado com semente no id da ligação: rodar de novo gera o mesmo áudio.

Uso:
    python sintetizar.py --saida ./audios
    python sintetizar.py --saida ./audios --somente sint_001,sint_019
"""

import argparse
import json
import random
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from kokoro import KPipeline

AQUI = Path(__file__).parent
SR = 24000

# Só três vozes do Kokoro são de português do Brasil. As misturas com vozes de
# espanhol e italiano dão timbres novos sem perder a pronúncia, que vem do
# fonemizador de português.
VOZES = {
    "f": ["pf_dora", "pf_dora,if_sara", "pf_dora,ef_dora"],
    "m": ["pm_alex", "pm_santa", "pm_alex,im_nicola", "pm_santa,em_alex"],
}

# Relação sinal-ruído (dB) e se há queda de sinal, por ambiente da consultora.
AMBIENTES = {
    "silencio": (34, False),
    "casa": (26, False),
    "rua": (17, True),
    "carro": (14, True),
}


def ruido_colorido(n: int, expoente: float, sorteio: np.random.Generator) -> np.ndarray:
    """Ruído com espectro 1/f^expoente: 1 é rosa, 2 é marrom."""
    espectro = np.fft.rfft(sorteio.standard_normal(n))
    freqs = np.fft.rfftfreq(n)
    freqs[0] = freqs[1]
    sinal = np.fft.irfft(espectro / freqs ** (expoente / 2), n)
    return sinal / (np.std(sinal) + 1e-9)


def rms_falado(canal: np.ndarray) -> float:
    ativo = canal[np.abs(canal) > 0.01]
    return float(np.sqrt(np.mean(ativo**2))) if len(ativo) else 0.1


def murmurio(fonte: np.ndarray, n: int, sorteio: np.random.Generator) -> np.ndarray:
    """Vozes ao fundo sem palavras: a própria ligação, invertida e defasada."""
    if len(fonte) < SR:
        return np.zeros(n)
    base = fonte[::-1]
    saida = np.zeros(n)
    for _ in range(4):
        deslocamento = int(sorteio.integers(0, len(base)))
        trecho = np.roll(base, deslocamento)
        saida += np.resize(trecho, n) * sorteio.uniform(0.5, 1.0)
    return saida / (np.std(saida) + 1e-9)


class Locutor:
    def __init__(self, pipeline: KPipeline, voz: str, velocidade: float, semitons: float):
        self.pipeline = pipeline
        self.voz = voz
        self.fator = 2 ** (semitons / 12)
        # O tom sobe reamostrando, o que também acelera; a velocidade pedida ao
        # Kokoro já desconta isso para a duração final ficar na velocidade sorteada.
        self.velocidade_kokoro = velocidade / self.fator

    def falar(self, texto: str) -> np.ndarray:
        partes = [
            np.asarray(audio, dtype=np.float32)
            for _, _, audio in self.pipeline(texto, voice=self.voz, speed=self.velocidade_kokoro)
        ]
        pausa = np.zeros(int(0.12 * SR), dtype=np.float32)
        audio = np.concatenate([x for p in partes for x in (p, pausa)][:-1]) if partes else pausa
        if abs(self.fator - 1) > 1e-3:
            n = int(len(audio) / self.fator)
            audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
        return audio


def montar_ligacao(roteiro: dict, pipeline: KPipeline) -> tuple[np.ndarray, dict]:
    sorteio = random.Random(roteiro["call_id"])
    gerador = np.random.default_rng(sorteio.randrange(2**32))

    voz_agente = sorteio.choice(VOZES[roteiro["agente_genero"]])
    opcoes = [v for v in VOZES[roteiro["consultora_genero"]] if v != voz_agente]
    voz_consultora = sorteio.choice(opcoes)
    ambiente = sorteio.choice(list(AMBIENTES))
    locutores = {
        "agente": Locutor(pipeline, voz_agente, sorteio.uniform(1.0, 1.12), sorteio.uniform(-1.0, 1.0)),
        "consultora": Locutor(pipeline, voz_consultora, sorteio.uniform(0.92, 1.08), sorteio.uniform(-1.5, 1.5)),
    }
    canal = {"agente": 0, "consultora": 1}

    trechos = []
    cursor = sorteio.uniform(0.6, 1.2)
    inicio_anterior = fim_anterior = 0.0
    for fala in roteiro["falas"]:
        audio = locutores[fala["quem"]].falar(fala["texto"])
        duracao = len(audio) / SR
        if fala["interrompe"] and trechos:
            inicio = max(inicio_anterior + 0.5 * (fim_anterior - inicio_anterior), fim_anterior - sorteio.uniform(0.4, 1.0))
        else:
            longa = sorteio.random() < 0.12
            inicio = cursor + (sorteio.uniform(1.2, 2.4) if longa else sorteio.uniform(0.18, 0.75))
        trechos.append((canal[fala["quem"]], inicio, audio))
        inicio_anterior, fim_anterior = inicio, inicio + duracao
        cursor = max(cursor, fim_anterior)

    total = int((cursor + sorteio.uniform(0.5, 1.0)) * SR)
    estereo = np.zeros((total, 2), dtype=np.float64)
    for c, inicio, audio in trechos:
        i = int(inicio * SR)
        estereo[i : i + len(audio), c] += audio

    # Lado da consultora: volume do celular, ambiente e quedas de sinal.
    consultora = estereo[:, 1]
    consultora *= sorteio.uniform(0.5, 1.35)
    snr, com_quedas = AMBIENTES[ambiente]
    nivel = rms_falado(consultora) / 10 ** (snr / 20)
    expoente = 2.0 if ambiente in ("rua", "carro") else 1.0
    consultora += ruido_colorido(total, expoente, gerador) * nivel
    if ambiente in ("casa", "rua"):
        consultora += murmurio(estereo[:, 0], total, gerador) * nivel * 0.8
    if com_quedas:
        for _ in range(sorteio.randint(2, 5)):
            i = sorteio.randrange(total)
            consultora[i : i + int(sorteio.uniform(0.04, 0.12) * SR)] = 0
    estereo[:, 1] = np.tanh(consultora * 1.2) / np.tanh(1.2)

    # Lado do agente: headset bom, com o murmúrio da central bem baixo.
    agente = estereo[:, 0]
    nivel = rms_falado(agente) / 10 ** (32 / 20)
    agente += ruido_colorido(total, 1.0, gerador) * nivel
    agente += murmurio(agente.copy(), total, gerador) * nivel * 1.5

    estereo /= max(1.0, np.max(np.abs(estereo)) / 0.95)
    detalhes = {
        "voz_agente": voz_agente,
        "voz_consultora": voz_consultora,
        "ambiente": ambiente,
        "duracao_s": round(total / SR, 1),
    }
    return estereo.astype(np.float32), detalhes


def para_telefone(estereo: np.ndarray, destino: Path) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        sf.write(tmp.name, estereo, SR, subtype="PCM_16")
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", tmp.name,
                "-af", "highpass=f=300,lowpass=f=3400",
                "-ar", "8000", "-ac", "2", "-c:a", "pcm_mulaw", str(destino),
            ],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--roteiros", default=str(AQUI / "roteiros.json"))
    parser.add_argument("--saida", default=str(AQUI / "audios"))
    parser.add_argument("--somente", default="", help="ids separados por vírgula")
    args = parser.parse_args()

    roteiros = json.loads(Path(args.roteiros).read_text(encoding="utf-8"))
    alvo = set(filter(None, args.somente.split(",")))
    saida = Path(args.saida)
    saida.mkdir(parents=True, exist_ok=True)

    dispositivo = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    pipeline = KPipeline(lang_code="p", repo_id="hexgrad/Kokoro-82M", device=dispositivo)
    print(f"Kokoro em {dispositivo}")

    referencia = []
    for roteiro in roteiros:
        if alvo and roteiro["call_id"] not in alvo:
            continue
        t0 = time.perf_counter()
        estereo, detalhes = montar_ligacao(roteiro, pipeline)
        para_telefone(estereo, saida / roteiro["filename"])
        referencia.append({**roteiro, "audio": detalhes})
        print(
            f"{roteiro['call_id']}  {detalhes['duracao_s']:5.1f}s de áudio em {time.perf_counter() - t0:5.1f}s  "
            f"{detalhes['ambiente']:<8} {detalhes['voz_agente']} x {detalhes['voz_consultora']}"
        )

    # Mesmo nome e mesmas chaves (filename, scenario, script) que o 00_setup_env
    # grava para os áudios gTTS: o notebook de comparação lê este arquivo para
    # calcular o WER, e o avaliar_gold.py usa o gabarito.
    caminho = saida / "_reference_scripts.json"
    anteriores = json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else []
    novos = {r["filename"] for r in referencia}
    juntos = [r for r in anteriores if r["filename"] not in novos] + referencia
    caminho.write_text(json.dumps(juntos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(referencia)} ligações em {saida}; gabarito em {caminho}")


if __name__ == "__main__":
    main()
