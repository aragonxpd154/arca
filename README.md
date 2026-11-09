# Arca

**Resgate setor a setor e clonagem de cartões CompactFlash para Linux (Zorin OS).**

Cartões CF velhos começam a falhar aos poucos: alguns setores deixam de ler, o cartão trava, some do leitor. O Arca tira do cartão tudo o que ainda é legível, isola os setores ruins com cuidado para não forçar o cartão, mostra o mapa de setores ao vivo e depois grava uma **cópia exata** num cartão novo, conferida por SHA-256.

![licença MIT](https://img.shields.io/badge/licença-MIT-blue) ![Linux](https://img.shields.io/badge/plataforma-Linux-informational) ![GTK4](https://img.shields.io/badge/GTK-4%20%2B%20libadwaita-4a86cf)

---

## O que ele faz

1. **Resgatar** — copia o cartão para um arquivo `.img`, setor a setor.
   - Lê **primeiro as estruturas de boot**: MBR e tabela de partições, boot sector (VBR), as duas FATs e o diretório raiz. Sem elas o cartão não monta nem dá boot, então elas vêm antes de tudo e são resolvidas setor a setor.
   - Faz várias passadas, na mesma estratégia do GNU ddrescue:

     | Passada | O que faz |
     |---|---|
     | Cópia rápida | Lê em blocos e **pula** as áreas com erro, para pegar o máximo de dados bons antes que o cartão piore |
     | Cópia reversa | Volta pelo que foi pulado, de trás para frente |
     | Aparar | Lê setor a setor as bordas de cada área com erro |
     | Raspar | Lê setor a setor o miolo das áreas com erro |
     | Novas tentativas | Relê os setores ruins N vezes, alternando a direção |

   - Lê com `O_DIRECT` (sem cache do kernel, que mascararia erros), desliga o read-ahead do cartão e reduz o timeout SCSI durante o resgate, restaurando tudo no final.
   - Salva o mapa continuamente (`.map`, **compatível com o GNU ddrescue**). Se o cartão desconectar ou você fechar o programa, basta clicar em **Retomar**: ele continua exatamente de onde parou.
   - Gera um **relatório** (`.relatorio.txt`) com a lista de setores ruins, o estado de cada estrutura de boot e o SHA-256 da imagem.

2. **Clonar para cartão novo** — grava a imagem em outro cartão (não precisa ser da mesma marca).
   - Confere se cabe. Cartões de mesma capacidade nominal ("1 GB") quase sempre têm números de setores diferentes entre marcas. O Arca compara o tamanho exato e aceita um cartão um pouco menor **se todas as partições couberem**.
   - Lê o cartão de volta e compara o SHA-256 com a imagem: se bater, a cópia é idêntica bit a bit.
   - Recusa gravar no disco do sistema e pede para digitar o nome do dispositivo (`sdc`, por exemplo) antes de apagar.

## O mapa de setores

| Cor | Significado |
|---|---|
| 🟩 verde | recuperado |
| verde-claro | parte do bloco já lida |
| ⬜ cinza | ainda não lido |
| 🟨 amarelo | erro na cópia em bloco, falta aparar |
| 🟧 laranja | falta raspar setor a setor |
| 🟥 vermelho | setor ruim |
| contorno azul | posição atual da leitura |

Os pontinhos brancos marcam onde ficam as estruturas de boot. Passe o mouse em qualquer quadrado para ver a faixa de setores e o estado de cada um.

## Instalação

Requisitos: Zorin OS 17 ou 18 (ou Ubuntu 22.04+/Debian 12+), Python 3.10+, GTK 4 e libadwaita.

**Opção 1 — instalador:**
```bash
git clone https://github.com/aragonxpd154/arca.git
cd arca
./install.sh
```

**Opção 2 — pacote .deb:**
```bash
./build-deb.sh
sudo apt install ./dist/arca_1.0.0_all.deb
```

Depois é só procurar **Arca** no menu do Zorin. Para remover: `./uninstall.sh` (ou `sudo apt remove arca`).

**Rodar sem instalar** (para desenvolver):
```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 librsvg2-common
python3 -m arca
```

## Como usar

1. Coloque o cartão gasto no leitor USB. Na aba **Resgatar**, ele aparece sozinho na lista.
2. Confira onde a imagem será salva (padrão: `~/Arca/`) e clique em **Iniciar resgate**. O sistema pede a senha de administrador uma vez.
3. Acompanhe o mapa. Ao terminar, clique em **Gravar em outro cartão**.
4. Troque pelo cartão novo, escolha-o na lista, confira o aviso de tamanho e clique em **Gravar no cartão**.

### Linha de comando

O mesmo motor funciona no terminal (útil por SSH ou em servidor):
```bash
sudo /usr/libexec/arca/arca-worker --texto listar
sudo /usr/libexec/arca/arca-worker --texto resgatar /dev/sdb ~/Arca/cartao.img --tentativas 5
sudo /usr/libexec/arca/arca-worker --texto gravar ~/Arca/cartao.img /dev/sdc
```

## Dicas para cartões que estão morrendo

- **Resgate primeiro, sempre.** Não tente consertar o sistema de arquivos (fsck, chkdsk) no cartão original: isso grava nele e pode destruir o que ainda dá para ler.
- Comece com poucas tentativas (3). Se sobrarem setores ruins, rode de novo com mais tentativas; o Arca só relê o que faltou.
- Se o leitor USB travar ou o cartão sumir do sistema, desconecte e reconecte e clique em **Retomar**. Às vezes trocar de leitor ou de porta USB recupera setores que antes falhavam.
- Setores ruins vão zerados para o cartão novo. O relatório diz se eles caíram numa estrutura de boot (grave) ou só na área de dados.
- Guarde o `.img`: ele é o backup do cartão. Dá para gravar quantas cópias quiser.
- Cartões CF industriais usam o modo True IDE. Num leitor USB comum eles aparecem como `/dev/sdX`; num adaptador CF-IDE/SATA também funcionam.

## Estrutura do projeto

```
arca/
├── arca/
│   ├── gui.py        interface GTK 4 + libadwaita (mapa de setores, páginas Resgatar e Clonar)
│   ├── worker.py     processo privilegiado (via pkexec) que lê e grava os cartões; também é a CLI
│   ├── rescue.py     motor de resgate em várias passadas
│   ├── mapfile.py    mapa de setores (formato ddrescue)
│   ├── bootinfo.py   análise de MBR, partições, boot sector e áreas FAT
│   ├── writer.py     gravação + verificação SHA-256 e checagem de tamanho
│   └── devices.py    descoberta de cartões (lsblk)
├── data/icons/       logo e ícones
├── packaging/        lançadores, atalho .desktop, regra polkit
├── tests/            testes com cartão simulado (setores ruins, instáveis, desconexão)
├── install.sh · uninstall.sh · build-deb.sh
```

A interface roda como usuário comum. Só o `arca-worker` roda como root (autorizado pelo polkit), e os dois conversam por JSON. Se a interface fechar, o worker salva o mapa e para com segurança.

## Testes

```bash
python3 -m unittest discover -s tests -v
```
Os testes simulam um cartão FAT16 com setores sempre ruins, setores instáveis (que leem na 2ª ou 3ª tentativa) e uma desconexão no meio do resgate, e verificam a retomada, a imagem final e a gravação verificada.

## Licença

MIT — © 2026 Marcos Santos
