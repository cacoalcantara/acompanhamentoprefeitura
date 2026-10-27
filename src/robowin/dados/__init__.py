"""M2: dados (ticks Parquet/DuckDB, banco SQLite WAL, qualidade, gerador sintetico)."""
from .banco import BancoSQLite
from .qualidade import (
    Achado,
    ConfigQualidade,
    RelatorioQualidade,
    verificar,
    verificar_livros,
    verificar_ticks,
)
from .sintetico import ROTULO_SINTETICO, ConfigSintetico, DadosSinteticos, gerar
from .ticks import ArmazenamentoTicks

__all__ = [
    "ROTULO_SINTETICO", "Achado", "ArmazenamentoTicks", "BancoSQLite", "ConfigQualidade", "ConfigSintetico",
    "DadosSinteticos", "RelatorioQualidade", "gerar", "verificar", "verificar_livros", "verificar_ticks",
]
