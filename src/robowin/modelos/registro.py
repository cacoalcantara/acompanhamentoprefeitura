"""Registro de modelos: versao, hashes de dados e codigo, treinado_ate, status, promocao e reversao.

- Versoes sao imutaveis; a fonte de verdade e um arquivo JSON simples (gravacao atomica).
  Se `banco` (Protocolo Banco, tabela `modelos` do esquema) for dado, cada registro novo e espelhado.
- Promocao a campeao exige: criterio OOS pre-registrado (hash igual ao do treino), metrica OOS registrada
  em periodo estritamente posterior ao treino, dados nao sinteticos/contaminados, aprovacao no criterio e
  superioridade sobre o campeao NO MESMO periodo.
- Reversao = troca de ponteiro para o campeao anterior (nenhum artefato e apagado).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from robowin.core.instrumentos import exigir_mesmo_ativo
from robowin.core.protocolos import Banco
from robowin.core.tipos import EntradaRegistroModelo

from .microestrutura import ORIGENS_SEM_SELO, CriterioOOS, ModeloMicroestrutura, hash_codigo


class ErroRegistro(RuntimeError):
    pass


class PromocaoRecusada(ErroRegistro):
    pass


class ModeloVazado(ErroRegistro):
    """Modelo treinado com rotulos que terminam depois do inicio do segmento em que seria avaliado."""


class RegistroModelos:
    def __init__(self, caminho: str | Path | None = None, banco: Banco | None = None):
        self.caminho = Path(caminho) if caminho else None
        self.banco = banco
        self._d: dict[str, Any] = {"modelos": {}}
        if self.caminho and self.caminho.exists():
            self._d = json.loads(self.caminho.read_text(encoding="utf-8"))

    # -- persistencia --------------------------------------------------------------------------
    def _salvar(self) -> None:
        if not self.caminho:
            return
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.caminho.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._d, f, indent=2, sort_keys=True)
        os.replace(tmp, self.caminho)

    def _grupo(self, nome: str) -> dict[str, Any]:
        return self._d["modelos"].setdefault(nome, {"campeao": None, "historico": [], "versoes": {}})

    def _versao(self, nome: str, versao: str) -> dict[str, Any]:
        try:
            return self._d["modelos"][nome]["versoes"][versao]
        except KeyError:
            raise ErroRegistro(f"versao inexistente: {nome}@{versao}") from None

    # -- consulta ------------------------------------------------------------------------------
    def entrada(self, nome: str, versao: str) -> EntradaRegistroModelo:
        return EntradaRegistroModelo(**self._versao(nome, versao)["entrada"])

    def exigir_ativo(self, nome: str, versao: str, ativo: str) -> EntradaRegistroModelo:
        """Recusa (AtivoDivergente) usar um modelo treinado em OUTRO ativo (ou sem ativo declarado)."""
        e = self.entrada(nome, versao)
        exigir_mesmo_ativo(ativo, e.ativo, f"modelo {nome}@{versao}")
        return e

    def campeao_para(self, nome: str, ativo: str) -> str | None:
        """Campeao do modelo SO se treinado neste ativo; de outro ativo => AtivoDivergente."""
        v = self.campeao(nome)
        if v is not None:
            self.exigir_ativo(nome, v, ativo)
        return v

    def campeao(self, nome: str) -> str | None:
        return self._d["modelos"].get(nome, {}).get("campeao")

    def historico(self, nome: str) -> list[str]:
        return list(self._d["modelos"].get(nome, {}).get("historico", []))

    def eventos(self, nome: str, versao: str) -> list[str]:
        return list(self._versao(nome, versao)["eventos"])

    def versoes(self, nome: str) -> list[str]:
        return sorted(self._d["modelos"].get(nome, {}).get("versoes", {}))

    def origem(self, nome: str, versao: str) -> str:
        return str(self._versao(nome, versao)["origem"])

    def artefato(self, nome: str, versao: str) -> dict[str, str] | None:
        """Arquivo e sha256 do artefato persistido desta versao (None se nao ha)."""
        a = self._versao(nome, versao).get("artefato")
        return dict(a) if a else None

    def registrar_artefato(self, nome: str, versao: str, arquivo: str, sha256: str) -> None:
        v = self._versao(nome, versao)
        if v.get("artefato"):
            raise ErroRegistro(f"artefato ja registrado (imutavel): {nome}@{versao}")
        v["artefato"] = {"arquivo": arquivo, "sha256": sha256}
        v["eventos"].append("artefato persistido")
        self._salvar()

    def oos(self, nome: str, versao: str) -> dict[str, dict[str, float]]:
        return dict(self._versao(nome, versao)["oos"])

    # -- registro ------------------------------------------------------------------------------
    def registrar(self, entrada: EntradaRegistroModelo, origem: str, criterio_hash: str = "") -> None:
        g = self._grupo(entrada.nome)
        if entrada.versao in g["versoes"]:
            raise ErroRegistro(f"versao ja registrada (imutavel): {entrada.nome}@{entrada.versao}")
        if entrada.status == "validado_oos":
            raise ErroRegistro("uma versao nao nasce validada: use registrar_oos + promover")
        g["versoes"][entrada.versao] = {
            "entrada": entrada.model_dump(), "origem": origem, "criterio_hash": criterio_hash,
            "oos": {}, "holdout_periodo": None, "eventos": ["registrada"],
        }
        self._salvar()
        if self.banco is not None:
            self.banco.inserir("modelos", {
                "nome": entrada.nome, "versao": entrada.versao, "status": entrada.status,
                "hash_dados": entrada.hash_dados, "hash_codigo": entrada.hash_codigo,
                "treinado_ate_ns": entrada.treinado_ate_ns, "metricas_json": json.dumps(entrada.metricas),
                "ativo": 0,
            })

    def registrar_modelo(self, modelo: ModeloMicroestrutura, versao: str) -> EntradaRegistroModelo:
        if modelo.status == "nao_treinado":
            raise ErroRegistro("modelo nao treinado")
        e = EntradaRegistroModelo(
            nome=modelo.nome, versao=versao, ativo=getattr(modelo, "ativo", ""), hash_dados=modelo.hash_dados, hash_codigo=hash_codigo(),
            status="em_validacao", treinado_ate_ns=modelo.treinado_ate_ns)
        self.registrar(e, origem=modelo.origem_dados, criterio_hash=modelo.hash_criterio_pre_registrado)
        return e

    def verificar_sem_vazamento(self, nome: str, versao: str, inicio_segmento_ns: int) -> None:
        e = self.entrada(nome, versao)
        if e.treinado_ate_ns > inicio_segmento_ns:
            raise ModeloVazado(
                f"{nome}@{versao} treinado ate {e.treinado_ate_ns}, depois do inicio do segmento {inicio_segmento_ns}")

    def registrar_oos(self, nome: str, versao: str, periodo: str, metricas: dict[str, float],
                      inicio_segmento_ns: int, criterio: CriterioOOS) -> bool:
        """Registra a metrica OOS de um periodo (uma vez por versao e periodo). Devolve se o criterio passou."""
        v = self._versao(nome, versao)
        self.verificar_sem_vazamento(nome, versao, inicio_segmento_ns)
        if periodo in v["oos"]:
            raise ErroRegistro(f"periodo {periodo!r} ja avaliado para {nome}@{versao}: holdout so uma vez")
        v["oos"][periodo] = dict(metricas)
        if v["holdout_periodo"] is None:
            v["holdout_periodo"] = periodo
        aprovado = False
        if v["criterio_hash"] and v["criterio_hash"] == criterio.hash():
            aprovado, _ = criterio.aprova(metricas)
            if not aprovado and v["entrada"]["status"] == "em_validacao":
                v["entrada"]["status"] = "reprovado"
                v["eventos"].append(f"reprovada em {periodo}")
        v["eventos"].append(f"oos registrado em {periodo}")
        self._salvar()
        return aprovado

    def reprovar(self, nome: str, versao: str, motivo: str) -> None:
        v = self._versao(nome, versao)
        v["entrada"]["status"] = "reprovado"
        v["eventos"].append(f"reprovada: {motivo}")
        self._salvar()

    # -- promocao e reversao -------------------------------------------------------------------
    def promover(self, nome: str, versao: str, periodo: str, criterio: CriterioOOS) -> None:
        v = self._versao(nome, versao)
        e = v["entrada"]
        if e["status"] != "em_validacao":
            raise PromocaoRecusada(f"status {e['status']!r}: so versoes em_validacao podem ser promovidas")
        if not v["criterio_hash"] or v["criterio_hash"] != criterio.hash():
            raise PromocaoRecusada("criterio nao pre-registrado (hash diferente do registrado no treino)")
        if v["origem"] in ORIGENS_SEM_SELO or v["origem"] in ("", "desconhecida"):
            raise PromocaoRecusada(f"dados de origem {v['origem']!r}: selo validado recusado")
        m = v["oos"].get(periodo)
        if m is None:
            raise PromocaoRecusada(f"sem metrica OOS registrada para o periodo {periodo!r}")
        ok, motivos = criterio.aprova(m)
        if not ok:
            raise PromocaoRecusada("criterio OOS nao atingido: " + "; ".join(motivos))
        g = self._grupo(nome)
        atual = g["campeao"]
        if atual is not None and atual != versao:
            mc = g["versoes"][atual]["oos"].get(periodo)
            if mc is None:
                raise PromocaoRecusada("campeao nao avaliado no mesmo periodo: comparacao impossivel")
            if not (m["exp_sel_brl"] > mc["exp_sel_brl"] and m["ganho_log_loss"] >= mc["ganho_log_loss"]):
                raise PromocaoRecusada("desafiante nao supera o campeao no mesmo periodo")
        if atual is not None and atual != versao:
            g["historico"].append(atual)
        e["status"] = "validado_oos"
        e["metricas"] = dict(m)
        g["campeao"] = versao
        v["eventos"].append(f"promovida a campeao com base em {periodo}")
        self._salvar()

    def reverter(self, nome: str) -> str | None:
        """Troca o ponteiro do campeao para o anterior. Devolve a versao que voltou a ser campea."""
        g = self._grupo(nome)
        if not g["historico"]:
            raise ErroRegistro("nao ha campeao anterior para reverter")
        saiu = g["campeao"]
        g["campeao"] = g["historico"].pop()
        if saiu is not None:
            g["versoes"][saiu]["eventos"].append("revertida (ponteiro trocado)")
        self._salvar()
        return g["campeao"]
