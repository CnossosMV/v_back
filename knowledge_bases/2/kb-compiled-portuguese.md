---
kb_type: compiled
product: Tabloide.pro
compiled_at: 2026-03-22
language: portuguese
source_files: 25
---

# Tabloide.pro — Base de Conhecimento Completa

> Este arquivo e a base de conhecimento oficial para o chatbot de suporte do Tabloide.pro.
> Gerado a partir do codigo-fonte em 2026-03-22. Regenerar apos cada lancamento de produto.

## Como usar este arquivo (para LLMs)

Voce e um chatbot de suporte do Tabloide.pro. Use esta base de conhecimento para responder perguntas dos usuarios com precisao.

**Regras:**
1. Responda apenas com base no que esta documentado aqui. Se algo nao estiver coberto, diga "Nao tenho essa informacao. Entre em contato com o suporte."
2. Quando um usuario perguntar sobre uma funcionalidade, verifique a secao de restricoes PRIMEIRO para ver se ha limitacoes.
3. Nunca diga que uma funcionalidade existe se estiver listada em "Acoes Estruturalmente Impossiveis".
4. Para perguntas sobre planos, use os dados da tabela de planos ativos.
5. Sempre forneca labels exatos de botoes e navegacao passo a passo.
6. Mensagens de erro devem ser citadas exatamente como documentado.

---

# GLOSSARIO

| Termo | Significado neste produto | Notas |
|-------|--------------------------|-------|
| Tabloide | O material de marketing criado (design com produtos e precos) | Nome principal do produto final |
| Template | Layout pre-desenhado com zonas para produtos | Criado por designers, usado por usuarios |
| Colecao | Grupo tematico de templates (ex: Natal, Dia das Maes) | Organiza templates por tema/ocasiao |
| Zona | Area dentro do template onde produtos podem ser posicionados | Cada zona comporta um ou mais produtos |
| Design | Instancia de um template preenchido pelo usuario | Salvo na conta do usuario |
| Workspace | Espaco de trabalho compartilhado (equivale ao cliente/loja) | Dono + membros da equipe |
| Cliente | Entidade que possui o workspace (geralmente o supermercado) | Usado internamente como "client" |
| Produto | Item de supermercado com imagem, nome e preco | Elemento principal do tabloide |
| Elemento de Produto | Componente visual do produto no design (imagem + nome + preco) | Posicionavel e editavel no canvas |
| Elemento de Preco | Componente visual do preco com estilizacao especifica | Tipografia e cores configuraveis |
| Derivativo de Preco | Variacao de layout do preco para diferentes contagens de digitos | Ex: layout diferente para R$1,99 vs R$19,99 |
| Grid Configuration | Configuracao de como produtos sao organizados em uma zona | Linhas, colunas, espacamento |
| Marca d'agua | Watermark adicionado a designs de planos trial | Removida em planos pagos |
| Validade | Datas de inicio e fim da oferta exibidas no tabloide | Data Inicial e Data Final |
| Logo | Logotipo da loja/marca | Upload ou selecionado da galeria |
| Loja | Localizacao fisica do supermercado com endereco e telefones | Dados aparecem no design |
| Fill Products | Funcao AI para buscar e adicionar produtos em lote | "Adicionar Meus Produtos" |
| Trocar Tema | Funcao para mudar o template mantendo os dados | Requer confirmacao (perde produtos) |
| Baixar Design | Exportar design como imagem PNG | Renderizacao server-side |
| Compartilhar | Enviar design via Web Share API ou copiar link | Tambem gera texto para WhatsApp/SMS |
| Modo Somente Leitura | Estado do editor quando assinatura expirada | Pode ver e baixar, mas nao editar |
| Trial | Plano gratuito temporario atribuido no registro | Limitado em credits, tempo e downloads |
| Credits | Quantidade de designs que podem ser criados | Decrementado a cada novo design |
| Waiver | Extensao temporaria de creditos ou tempo de assinatura | Administrado pelo suporte/admin |
| Onboarding | Processo guiado para novos usuarios | Informacoes do negocio, logo, loja |
| PIX | Metodo de pagamento instantaneo brasileiro | Gera QR code |
| Boleto | Metodo de pagamento brasileiro por boleto bancario | Gera PDF, 1-3 dias uteis |
| PWA | Progressive Web App - versao instalavel do aplicativo | Pode ser adicionada a tela inicial |
| Asaas | Provedor de pagamento brasileiro usado pela plataforma | Processa cartao, PIX e boleto |
| Inner Drag | Modo de arraste para reposicionar componentes dentro do elemento de produto | Permite mover imagem/nome/preco separadamente |
| Auto-save | Salvamento automatico do design a cada 30 segundos | Nao funciona em modo somente leitura |
| Oferta Especial | Link personalizado de pagamento com desconto | Acessado via /offer/:token |
| Descadastro | Opcao para parar de receber emails de marketing | Via link em emails enviados |
| Tipo de Comercio | Categoria do negocio (supermercado, padaria, acougue, etc.) | Definido no onboarding |

---

# RESTRICOES E LIMITES

## Planos Ativos

| Plano | Preco | Duracao | Credits de Design | Usuarios | Downloads/Design | Cobranca | Visivel ao Publico |
|-------|-------|---------|-------------------|----------|------------------|----------|--------------------|
| Experimental - Gratuito (Trial) | R$ 0 | 7 dias | 2 | ilimitado | 4 | unico | Nao |
| Free- Prospects | R$ 0 | 30 dias | 3 | ilimitado | 4 | - | Nao |
| Plano Basico Mensal | R$ 89,90 | 30 dias | 30 | 1 | 3 | mensal | Sim |
| Plano Pro Anual | R$ 718,80 | 365 dias | ilimitado | ilimitado | 3 | anual | Sim |

**Metodos de Pagamento (Apenas Planos Pagos):**
- Plano Basico Mensal: Cartao (R$ 79,90), PIX (R$ 89,90), Boleto (R$ 89,90) - sem parcelamento
- Plano Pro Anual: Cartao (R$ 718,80), PIX (R$ 718,80), Boleto (R$ 718,80) - parcelamento em 1, 3, 6 ou 12x

## Restricoes por Plano

| Funcionalidade | Experimental (Trial) | Basico Mensal | Pro Anual | Expirado |
|----------------|----------------------|---------------|-----------|----------|
| Criar design | 2 designs | 30 designs/mes | Ilimitado | Nao |
| Editar design | Sim (7 dias) | Sim (30 dias) | Sim (365 dias) | Somente leitura |
| Download | 4/design | 3/design | 3/design | Bloqueado |
| Equipe | Ilimitado | 1 usuario (sem equipe) | Ilimitado | Bloqueado |
| Auto-renovacao | Nao | Sim | Sim | N/A |
| Compartilhar | Sim | Sim | Sim | Sim |
| WhatsApp/SMS text | Sim | Sim | Sim | Sim |
| Lojas | Sim | Sim | Sim | Sim |
| Logos | Sim | Sim | Sim | Sim |

## Limites Rigidos

| Item | Limite | Quando excedido |
|------|--------|-----------------|
| Downloads por design (trial) | 4 | Toast: "Limite de Downloads Atingido" + redireciona /plans |
| Downloads por design (pago) | 3 | Toast: "Limite de Downloads Atingido" + redireciona /plans |
| Designs trial (Experimental) | 2 | Modal: "Limite de Assinatura Atingido" |
| Designs Basico Mensal | 30 por periodo | Modal: "Limite de Assinatura Atingido" |
| Designs Pro Anual | Ilimitado | - |
| Usuarios Basico Mensal | 1 (sem equipe) | Nao pode convidar membros |
| Telefones por loja (display) | 2 | Apenas 2 telefones exibidos |
| Zoom editor | 25% - 500% | Botoes desabilitados nos limites |
| Senha minima | 8 caracteres | Validacao no formulario |
| Auto-save intervalo | 30 segundos | Fixo |
| Parcelamento maximo | 12x (apenas Pro Anual) | - |

## Acoes Estruturalmente Impossiveis

O chatbot NUNCA deve dizer "sim" para estas acoes:

- **Editar templates diretamente:** Usuarios nao podem modificar a estrutura dos templates. Apenas designers/admins criam templates.
- **Importar designs de outras plataformas:** Nao ha importacao de designs externos (Canva, Photoshop, etc.).
- **Exportar em PDF ou vetor:** Exports sao apenas PNG. Nao ha PDF, SVG ou outros formatos.
- **Criar templates customizados:** Apenas admins/designers podem criar templates.
- **Pagamento em dolar/euro:** Apenas BRL (Real brasileiro) suportado.
- **Pagamento via PayPal/Stripe:** Apenas Asaas (Cartao, PIX, Boleto).
- **Compartilhar design editavel entre workspaces:** Designs sao do workspace, nao compartilhaveis.
- **Undo/Redo via botao:** Nao ha botoes visiveis. Historico reseta ao trocar template.
- **Colaboracao em tempo real:** Nao ha edicao simultanea.
- **Agendamento de publicacao em redes sociais:** Nao existe.
- **Integracao direta com redes sociais:** Compartilhamento e via Web Share API ou copy/paste.

## Tipos de Comercio Disponiveis

Acougue, Farmacia, Loja de Materiais de Construcao, Loja de Roupas, Loja de Sapato, Mercadinho, Mercearia, Otica, Outras, Padaria, PetShop, Sacolao/Hortifruti, Supermercado

## Mensagens de Erro Conhecidas

| Mensagem | Quando aparece | O que o usuario deve fazer |
|----------|----------------|---------------------------|
| "Modo Somente Leitura" | Assinatura expirada | Fazer upgrade em /plans |
| "Limite de Assinatura Atingido" | Credits esgotados | Escolher um plano pago |
| "Servico temporariamente indisponivel" | Erro de servidor | Aguardar e tentar novamente |
| "Erro de conexao" | Sem internet | Verificar conexao |
| "Limite de Downloads Atingido" | Downloads excedidos | Fazer upgrade |
| "Sua assinatura expirou" | Download com assinatura expirada | Fazer upgrade |
| "Download Temporariamente Indisponivel" | Erro no servidor | Aguardar e tentar novamente |
| "Falha ao compartilhar" | Erro no compartilhamento | Verificar imagens e tentar |
| "Falha ao salvar o design" | Erro generico de save | Tentar novamente |
| "Nome da loja e obrigatorio" | Onboarding sem nome | Preencher campo |
| "Telefone e obrigatorio" | Onboarding sem telefone | Preencher campo |
| "Limpar todos os produtos?" | Confirmacao de limpeza | Confirmar ou cancelar |
| "Tem certeza que deseja trocar de tema?" | Confirmacao de troca | Salvar antes ou descartar |

---

# FUNCIONALIDADES

## Editor de Design

Editor visual estilo Canva onde o usuario cria materiais de marketing para supermercados, posicionando produtos com imagens, nomes e precos em templates pre-desenhados.

**Como acessar:** Na pagina inicial, clique em "Comecar Novo Tabloide" > Escolha colecao e template > Editor abre

**Funciona quando:** Todos os planos (trial e pago), autenticado

**Nao funciona quando:**
- Assinatura expirada: Editor abre em modo somente leitura (banner laranja)
- Credits esgotados (trial): Nao pode criar novos designs

### Acoes da Toolbar

| Acao | Label | Descricao |
|------|-------|-----------|
| Home | Voltar para Inicio | Volta para pagina inicial |
| Zoom | Aumentar/Diminuir zoom | 25%-500%, incremento 10% |
| Produtos | Adicionar Produto | Abre modal de busca AI |
| Limpar | Limpar Todos | Remove todos produtos (com confirmacao) |
| Logo | Logo | Upload ou selecionar da galeria |
| Loja | Alterar Dados da Loja | Mudar loja selecionada |
| Validade | Validade | Definir datas de validade |
| Tema | Trocar Tema | Trocar template (com confirmacao) |
| Imagem | Adicionar Imagem | Upload de imagem customizada |
| Texto | Adicionar Texto | Adicionar texto customizado |
| Download | Baixar Design | Download PNG alta qualidade |
| Compartilhar | Compartilhar | Web Share API ou copiar link |
| WhatsApp | Texto para WhatsApp | Texto AI gerado para WhatsApp |
| SMS | Texto para SMS | Texto AI gerado para SMS |

### Atalhos de Teclado

| Atalho | Acao |
|--------|------|
| Delete/Backspace | Deletar elemento selecionado |
| Ctrl/Cmd + A | Abrir modal Adicionar Produto |
| Ctrl/Cmd + D | Redistribuir produtos automaticamente |
| Space + Drag | Mover canvas (pan) |

### Interface Mobile

- Botoes flutuantes: Home (amarelo), Download/Share (vermelho), Aprenda (roxo)
- Menu inferior slide-up: MOVER, EDITAR, CONFIG, DELETAR, Cancelar
- Controles de zoom touch-friendly na parte inferior
- Sem sidebar fixa

### Modo Somente Leitura

- Banner laranja: "Modo somente leitura: Voce pode visualizar e baixar, mas nao editar."
- Botoes disponiveis: "Baixar", "Upgrade", "Sair"
- Botoes desabilitados: Todas as acoes de edicao

### Auto-Save

- Intervalo: 30 segundos
- Automatico quando mudancas detectadas
- Desabilitado em modo somente leitura

---

## Selecao de Templates

Permite navegar e escolher templates organizados em colecoes para criar materiais de marketing.

**Formatos disponiveis:** Quadrado, Story, Post, Folheto, Retrato, Paisagem

**Filtros:** Busca por texto, tags (checkbox), formato (checkbox), botao "Limpar"

**Fluxos:**
1. Template Selector (/template-selector): Navegacao completa por colecoes
2. New Creation Flow (/new-creation-flow): Fluxo simplificado
3. Templates Gallery (/templates): Galeria com filtros avancados

---

## Produtos no Editor

Permite adicionar, posicionar e editar produtos com imagens, nomes e precos.

**Como acessar:** No editor, "Adicionar Produto" ou Ctrl+A

**Acoes:** Adicionar, selecionar, mover (drag), redimensionar, rotacionar, editar visual, configurar, substituir ("Trocar Produto"), deletar, limpar todos, redistribuir (Ctrl+D)

**Fill Products Modal:** Busca AI de produtos, remocao de fundo automatica, modo substituicao para trocar produto individual

---

## Download e Compartilhamento

**Opcoes:**
- Baixar Design: Download PNG alta qualidade (renderizacao server-side)
- Compartilhar: Web Share API (mobile) ou copiar link (desktop)
- Texto para WhatsApp: Texto AI gerado com produtos
- Texto para SMS: Texto AI gerado com contagem de caracteres

**Limites de download:**
- Trial (Experimental): 4 por design
- Planos pagos: 3 por design

---

## Gerenciamento de Lojas

Permite gerenciar locais de supermercado com enderecos e telefones.

**Como acessar:** Menu lateral > "Lojas" ou /stores

**Campos:** Nome (obrigatorio), endereco (obrigatorio), telefone 1 (opcional, formato brasileiro), WhatsApp 1, telefone 2, WhatsApp 2

**Acoes:** Criar, editar inline, definir padrao (estrela), deletar, adicionar/editar telefone

---

## Galeria de Logos

Upload, organizacao e gerenciamento de logos da marca.

**Como acessar:** Menu lateral > "Logos" ou /logos

**Acoes:** Upload (modal), visualizar detalhes, editar (LogoImageEditor com remocao de fundo), definir padrao, deletar

**Grid:** 2-6 colunas, scroll infinito (24 itens por pagina)

---

## Planos e Pagamento

Sistema de assinatura com planos trial e pagos.

**Metodos de pagamento:** Cartao de Credito (ate 12x para Pro Anual), PIX (QR code, imediato), Boleto (PDF, 1-3 dias)

**Provedor:** Asaas (fintech brasileira)

**Status da assinatura:**
- Ativo Pago: Acesso completo, renovacao automatica
- Ativo Trial: Credits/tempo limitados, sem renovacao
- Expirado: Somente leitura, necessita upgrade
- Cancelado: Valido ate end_date, sem renovacao

**Waivers:** Extensao temporaria de creditos ou tempo (administrado pelo suporte)

**Ofertas Especiais:** Links personalizados /offer/:token com descontos exclusivos

---

## Autenticacao e Conta

**Login:** Email/Senha, Google, Microsoft, Facebook, Instagram

**Requisitos de senha:** 8+ caracteres, 1 maiuscula, 1 minuscula, 1 especial, 1 digito

**Perfil:** Dados pessoais (nome, email) + Senha (apenas login email)

**Configuracoes:** Dados do Cliente, Equipe, Assinatura, Pedidos, Notificacoes

**Roles:** user, designer (bypass assinatura), admin (bypass assinatura), system_admin

---

## Equipe e Colaboracao

Convide membros para o workspace por email.

**Roles:** owner (tudo), admin (quase tudo), user (editar designs)

**Limites:** Plano Basico Mensal: 1 usuario (sem equipe). Pro Anual: ilimitado.

**Convites:** Enviados por email, aceitos via link /invitations/accept/:token

---

## Onboarding

Sequencia guiada para novos usuarios:
1. Informacoes do negocio (nome, telefone, tipo comercio, frequencia)
2. Selecao de template
3. Welcome Modal: "Adicionar Meus Produtos" (AI) ou "Comecar com Exemplos"
4. Logo upload/selecao
5. Dados da loja (endereco, telefone)
6. Validade (datas)
7. Design Pronto (trial): "Seu tabloide esta pronto para uso!"

---

## Preferencias de Email

Gerenciar preferencias e descadastrar-se de comunicacoes.

**Opcoes:** Todos os emails, apenas promocionais, apenas desta campanha

**Acesso:** Link "descadastrar" em qualquer email recebido

---

# FLUXOS

## Criar Novo Design

1. Na pagina inicial, clique em "Comecar Novo Tabloide"
2. Navegue pelas colecoes de templates
3. Selecione um template
4. No Welcome Modal, escolha modo de adicionar produtos
5. Adicione produtos (AI ou manual)
6. Ajuste posicao e tamanho
7. Adicione logo e dados da loja
8. Defina datas de validade
9. Design salva automaticamente
10. Clique em "Baixar Design" para exportar

**Falhas:** Limite atingido > Modal upgrade. Expirado > Bloqueado. Erro rede > Toast.

## Editar Design Existente

1. Na pagina inicial, clique no icone de edicao do design
2. Faca as modificacoes desejadas
3. Design salva automaticamente
4. Baixe a versao atualizada

**Falhas:** Expirado > Modo somente leitura com banner.

## Baixar Design

1. No editor, "Baixar Tabloide" > "Baixar Design"
2. Aguarde processamento (fila mostra posicao/tempo)
3. Download inicia automaticamente

**Falhas:** Trial 4+ downloads > "Limite de Downloads Atingido". Servidor > "Download Temporariamente Indisponivel".

## Criar Conta

1. Acesse /register
2. Preencha nome, email, senha
3. Aceite termos
4. Verifique email
5. Preencha informacoes do negocio
6. Trial atribuido automaticamente

## Fazer Upgrade

1. Acesse /plans
2. Compare planos
3. Escolha metodo de pagamento
4. Complete checkout
5. Assinatura ativada apos confirmacao

## Gerenciar Lojas

1. Acesse /stores
2. "Nova Loja" ou edite existentes
3. Preencha dados
4. Defina padrao com estrela

## Gerenciar Logos

1. Acesse /logos
2. "Novo Logo" para upload
3. Edite ou defina como padrao

## Compartilhar via WhatsApp

1. No editor, "Baixar Tabloide" > "Texto para WhatsApp"
2. Texto AI gerado com produtos/precos
3. Copie ou abra WhatsApp Web

## Convidar Membro

1. Configuracoes > Equipe > Convidar
2. Digite email e selecione role
3. Convite enviado por email
4. Membro aceita via link

## Trocar Template

1. No editor, Opcoes > "Trocar Tema"
2. Confirme (produtos serao perdidos)
3. Selecione novo template
4. Editor carrega novo template

---

# PERGUNTAS FREQUENTES

## Primeiros Passos

### Como criar minha conta no Tabloide.pro?
Acesse a pagina de registro, preencha seu nome, email e crie uma senha (minimo 8 caracteres com maiuscula, minuscula, numero e caractere especial). Voce tambem pode usar login social com Google. Apos o registro, verifique seu email.

### O que acontece quando eu crio minha conta?
Voce recebe automaticamente um plano trial gratuito (Experimental) com 7 dias e 2 designs inclusos. Na primeira vez, um formulario pedira informacoes do seu negocio.

### Quais tipos de comercio sao suportados?
Acougue, Farmacia, Loja de Materiais de Construcao, Loja de Roupas, Loja de Sapato, Mercadinho, Mercearia, Otica, Padaria, PetShop, Sacolao/Hortifruti, Supermercado, e Outras.

### Como faco meu primeiro tabloide?
Na pagina inicial, clique em "Comecar Novo Tabloide". Escolha um template e use "Adicionar Meus Produtos" para buscar imagens por AI.

### Preciso instalar alguma coisa?
Nao. Funciona no navegador. Voce pode adicionar a tela inicial do celular como PWA.

## Acoes Comuns

### Como adicionar produtos ao meu tabloide?
No editor, clique em "Adicionar Produto" ou Ctrl+A. Cole uma lista de produtos para busca AI ou adicione individualmente.

### Como alterar o preco de um produto?
Clique no produto no canvas e edite o preco no painel de propriedades.

### Como trocar o template?
No editor, Opcoes > "Trocar Tema". Atencao: produtos serao removidos.

### Como adicionar minha logo?
No editor, menu "Logo". Upload ou selecione da galeria. Logo padrao e aplicada automaticamente.

### Como baixar meu tabloide?
"Baixar Tabloide" > "Baixar Design". PNG de alta qualidade.

### Posso compartilhar pelo WhatsApp?
Sim! "Baixar Tabloide" > "Texto para WhatsApp". Texto gerado por AI com produtos e precos.

### O design salva automaticamente?
Sim, a cada 30 segundos. Nao precisa salvar manualmente.

### Como convidar membros?
Configuracoes > Equipe > Convidar usuario por email.

## Limitacoes

### Quantos tabloides posso criar no trial?
2 designs em 7 dias.

### Quantas vezes posso baixar o mesmo design?
Trial: 4 vezes. Pagos: 3 vezes por design.

### Posso editar depois que expirar?
Nao. Editor fica em "Modo Somente Leitura". Faca upgrade.

### Posso exportar em PDF?
Nao. Apenas PNG.

### Posso criar meus proprios templates?
Nao. Templates sao criados por designers profissionais.

### Posso importar de outras plataformas?
Nao. Nao ha importacao de Canva, Photoshop, etc.

### Posso agendar publicacoes?
Nao. Baixe o design e publique manualmente.

### Posso colaborar em tempo real?
Nao. Nao ha edicao simultanea.

### Posso ter equipe no Basico Mensal?
Nao. Apenas 1 usuario. Para equipe, use Pro Anual.

## Planos e Precos

### Quais planos disponiveis?
- Plano Basico Mensal: R$ 89,90/mes, 30 designs, 1 usuario
- Plano Pro Anual: R$ 718,80/ano (ate 12x), ilimitado

### Quais formas de pagamento?
Cartao, PIX e Boleto (Asaas).

### Posso parcelar?
Sim, Pro Anual ate 12x no cartao. Basico nao tem parcelamento.

### PIX e mais barato?
Basico Mensal: Cartao R$ 79,90 vs PIX/Boleto R$ 89,90. Pro Anual: mesmo preco.

### Como cancelo?
Configuracoes > Assinatura > Cancelar. Acesso ate a data de expiracao.

## Erros e Problemas

### "Modo Somente Leitura" - O que fazer?
Assinatura expirou. Clique em "Upgrade" para escolher um plano.

### "Limite de Assinatura Atingido" - O que fazer?
Credits esgotados. Faca upgrade para mais designs.

### "Limite de Downloads Atingido" - O que fazer?
Downloads excedidos neste design. Faca upgrade ou use "Compartilhar".

### Download esta demorando.
Se houver fila, aguarde. Se "Download Temporariamente Indisponivel", tente em minutos.

### Nao consigo salvar.
Verifique assinatura e conexao. Auto-save funciona a cada 30 segundos.

### Nao consigo fazer login.
Verifique email/senha. Se esqueceu, use "Esqueceu a senha?". Se usou Google, use login social.

### Email de verificacao nao chegou.
Verifique spam. Tente fazer login novamente para reenviar.
