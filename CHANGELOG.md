# Histórico de versões

## 1.1.0 — 09/11/2026

**Novo: aba Diagnóstico**
- Teste **só leitura** (seguro) e **leitura e escrita** (destrutivo), com quantas passadas quiser ou no modo **até falhar**.
- Padrões de escrita por passada: aleatório, `0x55`, `0xAA`, `0xFF`, `0x00`, cada setor carimbado com o próprio endereço.
- Detecta bits corrompidos, escrita perdida, capacidade falsa, leitura instável, blocos lentos e degradação entre passadas.
- Mapa de latência por bloco, velocidade ao longo do cartão, velocidade por passada e histórico da unidade.
- Nota de saúde de 0 a 100 com veredito e explicação de cada desconto.
- Relatório `.txt` e dados por bloco em `.csv`; histórico em `~/.local/share/arca/diagnosticos.json`.
- Linha de comando: `arca-worker diagnosticar`.

**Visual e instalação**
- Logo, banner e prints no README.
- Atalho com o logo na Área de Trabalho ao instalar; ícones em PNG para todos os tamanhos do menu.

**Correções**
- A gravação não falha mais quando o `partprobe` não está instalado.

## 1.0.0 — 09/11/2026

- Primeira versão: resgate setor a setor com mapa ao vivo, estruturas de boot primeiro, mapa compatível com o ddrescue, retomada, relatório.
- Clonagem para cartão novo com checagem de tamanho por partição e verificação SHA-256.
