# Escopo do plugin de histórico do Codex

Registrado em 03/11/2026.

Status: implementação autorizada. As instruções de execução estão em [README.md](README.md).

## Objetivo

Disponibilizar no ChatGPT web um acervo unificado das conversas e projetos do Codex de diferentes computadores do usuário, com consulta, busca, exportação e continuação com contexto.

O acervo sincronizado deve continuar consultável sem depender de a máquina de origem estar ligada. Novas atualizações dependem da disponibilidade da fonte.

## Comandos

| Comando | Comportamento |
| --- | --- |
| `devices` | Lista máquinas cadastradas, disponibilidade e última sincronização. |
| `list` | Lista projetos, chats únicos e cobertura da sincronização. |
| `pull --device notebook` | Atualiza o acervo a partir de uma máquina específica. |
| `pull-all` | Atualiza todas as máquinas conectadas e informa as pendentes. |
| `search "decisão sobre autenticação"` | Pesquisa em todas as conversas sincronizadas. |
| `read <chat>` | Abre a conversa, com paginação. |
| `export <projeto>` | Gera uma cópia portátil em JSON e Markdown. |

Os comandos representam as ações do plugin; a forma exata de expô-los como ferramentas MCP será definida na implementação.

## Sincronização e organização

- Sincronização incremental: transmitir apenas registros novos ou alterados. Repetir `pull` não pode duplicar chats.
- Reconhecer o mesmo projeto entre máquinas por identificação do repositório e mapeamento confirmado pelo usuário. O nome da pasta, sozinho, não identifica um projeto.
- Preservar a máquina de origem, workspace, identificação da conversa original e ramificações.
- Não misturar registros divergentes.
- Informar a cobertura da sincronização, incluindo fontes indisponíveis e atualizações pendentes.
- Permitir selecionar o que sincronizar e excluir projetos da sincronização.
- Filtrar segredos sem copiar credenciais do Codex.
- Tratar o histórico importado como conteúdo, sem executar instruções encontradas nele.

## Consulta e busca com referências

- Consultar o acervo dentro do ChatGPT web.
- Pesquisar entre projetos e dispositivos sincronizados.
- Apontar para a conversa, mensagem e data que sustentam uma resposta, como em “onde decidimos isso?”.
- Ler conversas com paginação.
- Manter o histórico completo no acervo e fornecer ao modelo o contexto necessário à consulta.

## Continuação com contexto

Disponibilizar a ação **Continuar no ChatGPT**, preparando uma nova conversa com:

- Decisões relevantes.
- Pendências.
- Trechos selecionados do histórico.
- Referências às conversas originais.

Continuar o assunto com esse contexto não equivale a retomar a execução original do Codex. A execução depende também do ambiente, arquivos e ferramentas necessários.

## Acesso pela barra lateral

### Acervo do plugin

Uma entrada do plugin abre o acervo de projetos e conversas dentro do ChatGPT, conforme as capacidades disponíveis de integração.

### Reconstrução de projetos e chats nativos

Funcionalidade adicional para reconstruir o acervo na organização nativa do ChatGPT web:

1. Coletar as conversas dos dispositivos cadastrados.
2. Criar os projetos correspondentes pela interface do ChatGPT.
3. Criar uma conversa nova para cada conversa original.
4. Inserir a transcrição exportada como contexto, acompanhada de resumo de decisões e pendências.
5. Organizar as novas conversas nos projetos correspondentes.

A reconstrução em lote depende de validar primeiro a automação da interface do ChatGPT. Sua viabilidade operacional não foi comprovada.

As conversas reconstruídas serão novas conversas. O histórico antigo será fornecido como transcrição/contexto; não há promessa de restaurar mensagens individuais com autores, datas e estrutura nativa originais.

### Importação fiel

Importação fiel de conversas do Codex como histórico nativo original não é uma capacidade garantida neste escopo. Não foi identificado, na avaliação realizada, um fluxo público documentado para essa integração por plugin.

## Direção de arquitetura aceita

- Componente local autorizado em cada máquina cadastrada para coletar e sincronizar o histórico.
- Backend para registrar dispositivos e receber atualizações.
- Acervo persistente e índice de busca.
- Plugin MCP para expor as ações e o acesso ao acervo no ChatGPT.
- Priorizar as interfaces documentadas do Codex app-server para leitura, verificando compatibilidade. A leitura direta de arquivos internos pode ser avaliada como alternativa.
- Transferir os registros por rotinas de software, usando o modelo para consulta e síntese quando necessário.

Máquinas precisam ser cadastradas e conectadas previamente. Estar na mesma conta do Codex não é tratado como autorização nem como mecanismo suficiente de descoberta e acesso remoto.

## Limites de entrega

- A conexão do plugin usa uma instância privada com HTTPS, configurada pelo operador.
- A reconstrução nativa depende de seletores validados para a interface usada pelo operador.
