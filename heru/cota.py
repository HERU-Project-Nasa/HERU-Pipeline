import time
from collections import deque

from heru.config import JANELA_COTA_S, LIMITE_SEGURO


class Cota:
    def __init__(self, limite=LIMITE_SEGURO, janela_s=JANELA_COTA_S,
                 relogio=time.monotonic, dormir=time.sleep, aviso=print):
        self.limite = limite
        self.janela_s = janela_s
        self.relogio = relogio
        self.dormir = dormir
        self.aviso = aviso
        self.registro = deque()
        self.total = 0

    def limpar(self):
        agora = self.relogio()
        while self.registro and agora - self.registro[0][0] >= self.janela_s:
            self.registro.popleft()
        return agora

    def usado(self):
        self.limpar()
        return sum(custo for _, custo in self.registro)

    def esperar(self, custo):
        if custo > self.limite:
            raise ValueError(f"custo {custo} maior que o limite {self.limite}")
        while True:
            agora = self.limpar()
            if sum(c for _, c in self.registro) + custo <= self.limite:
                return
            espera = self.registro[0][0] + self.janela_s - agora + 1
            self.aviso(f"cota: {self.usado()} de {self.limite} em 10 min; esperando {espera:.0f} s")
            self.dormir(espera)

    def registrar(self, custo):
        self.registro.append((self.relogio(), custo))
        self.total += custo

    def sincronizar(self, atual_firms):
        diferenca = atual_firms - self.usado()
        if diferenca > 0:
            self.registro.append((self.relogio(), diferenca))
        return diferenca
