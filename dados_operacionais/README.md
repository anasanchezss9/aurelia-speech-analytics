# Dados operacionais sintéticos — Aurélia Cosméticos

A Aurélia é uma empresa fictícia de venda direta. Esta pasta gera o que o sistema
de pedidos e o de atendimento dela teriam, para o build ter volume de verdade em
volta das 30 ligações com gabarito de `sintetico/`: o Genie precisa de dado para
responder, e o modelo de fraude precisa de histórico para aprender.

```bash
pip install numpy pandas pyarrow scikit-learn
python gerar_dados.py --saida ./saida          # ~15 s, 20 mil consultoras
python checar_sinal.py --dados ./saida         # confere a história antes de subir
databricks fs cp -r ./saida dbfs:/Volumes/<catalog>/<schema>/operacional -p <perfil>
```

`_gabarito/` traz a verdade que o mundo real não tem (quem é fraudadora, qual foi a
causa de cada chamado). Ela serve para medir o modelo e **não** deve ser lida pelo
pipeline.

## O que sai

| Tabela | Linhas | O que é |
|---|---|---|
| `consultoras` | 20 mil | UF, região, data de cadastro, nível |
| `produtos` | 45 | catálogo fictício (Âmbar, Maré, Raízes, Diário, Aura, Tempo…), preço e peso |
| `pedidos` | ~211 mil | 24 ciclos de 21 dias, com CD e transportadora |
| `pedido_itens` | ~1,4 mi | itens e preço unitário |
| `entregas` | ~211 mil | datas, status (inclui `em_transito` para o que não chegou até o corte), peso teórico, peso na expedição e no hub, comprovante |
| `chamados` | ~9,9 mil | motivo declarado, canal, valor reclamado, status, valor ressarcido |
| `chamado_itens` | ~7,3 mil | o que a consultora diz que faltou |
| `investigacoes` | ~2 mil | resultado da investigação — o único rótulo que existe |
| `ligacoes` | 1.230 | 30 com áudio sintético (`sint_NNN.wav`) e 1.200 a transcrever como texto |

A data de corte é fixa (21/09/2026), e não `datetime.now()`: o mundo é o mesmo a
cada geração com a mesma semente, então os números do deck não mudam quando o dado
é regenerado.

## A história plantada

Cada causa de falta deixa uma assinatura no peso da caixa, que é pesada em dois
pontos:

| Causa | Peso na expedição | Peso no hub | Outros sinais |
|---|---|---|---|
| Erro de separação | abaixo do teórico | igual à expedição | itens baratos, aleatórios |
| Furto em trânsito | igual ao teórico | abaixo da expedição | itens caros, rota específica |
| Fraude da consultora | igual ao teórico | igual à expedição | comprovante assinado, perfumaria, reincidência, pouco tempo de casa |

Duas ondas sobem a taxa de chamados **sem** haver fraude, e são o que derruba
quem olha só para a taxa:

- **Furto na TransLeste no RJ** a partir do ciclo 14: de ~4 para ~11 chamados por 100 pedidos.
- **Troca de WMS no CD Contagem** nos ciclos 9 a 11: de ~4,5 para ~8.

A fraude é ~10% dos chamados: 1,2% das consultoras com perfil fraudador (mais
comum em quem tem menos de um ano de casa) e fraude oportunista rara em quem não
tem perfil. Para não ser fácil demais, 45% das caixas não são pesadas no hub, a
balança tem ruído, e quatro em cada dez fraudes reclamam um item só e barato, para
ficar abaixo do valor que dispara investigação.

## O rótulo tem viés, e isso é de propósito

Só ~20% dos chamados fechados são investigados, e não ao acaso: a política
investiga mais chamado acima de R$ 200, consultora com dois ou mais chamados em 180
dias e caixa violada. Por isso a fraude confirmada é 14,4% dos investigados e 10%
de todos os chamados. Um modelo treinado nesse rótulo aprende a fraude que a
investigação costuma encontrar; a que escapa da política fica sub-representada.
Os chamados das últimas três semanas estão abertos e sem rótulo — são a fila que
o modelo ordena.

## Resultado do `checar_sinal.py` (semente 42)

| Medida | Valor |
|---|---|
| Teste temporal (25% mais recentes dos investigados) | PR-AUC 0,83 · ROC-AUC 0,96 · prevalência 14,2% |
| Fila aberta, 50 primeiros pelo score | 88% de fraude real, contra 10% na fila inteira |
| Chamados das 5 ligações de fraude | scores de 0,56 a 0,97; os outros 25 abaixo de 0,05 |

O modelo de referência do script é só uma checagem de que o padrão existe; o
modelo do build é treinado e servido no Databricks.
