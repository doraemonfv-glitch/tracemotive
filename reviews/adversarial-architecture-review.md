結論から言うと、AgentLensのKernelはかなり強い。ただし「将来の因果デバッガ」まで逆算すると、**実行時の証拠を後から欠損で上書きできる lifecycle merge semantics**だけは、Issue 01前に直す価値が高い。もう1点、OpenAI adapterが取得可能なLLM request configurationを明示的に捨てる部分も、再現性の観点では再検討価値がある。

# 1. Executive verdict

## **B — strong but has fixable future blockers**

Aにかなり近いB。

現在のKernelには将来性の高い判断がすでに入っている。

-  Canonical IDとframework native IDを分離している 
-  Framework → Adapter → Canonicalという境界が強い 
-  Span identityが安定している 
- `input/output` がgeneric JSONでprovider objectに固定されていない 
- `details` と `attributes` が役割分離されている 
-  content absenceを単なる`null`ではなくCaptureInfoで表現している 
-  parentが欠損してもSpanを受理できる 
-  out-of-order ingestを前提にしている 
-  OpenTelemetry schemaそのものをCanonicalにしていない 
-  Query API / SQLite / Adapterが疎結合 
-  future causal graphを**Span schemaの中に無理やり押し込まなくていい** 

これはかなり重要。

将来、

> Span Tree → Causal Evidence Graph

へ進化するとき、今のSpanを捨てる必要はない。

```
```

```
Trace
 └─ Span
      ↑
      │
Future Evidence Node / Causal Edge / Experiment / Fork
```

という**追加レイヤー**で伸ばせる。

一方で、現在1つ本質的な問題がある。

> v0.1はeventを受け取った後、そのeventそのものを保存せず、最終entity snapshotへmergeする。

そのため、**早い段階で観測できた証拠を、後の欠損snapshotで消失させる経路が存在する。**

これは「後でschemaを足せばいい」問題ではない。

一度消した値は未来のAgentLensから復元できない。

---

# 2. Future Capability Matrix

| CapabilityCurrent supportFuture migration difficultyIrrecoverable-information riskRequired v0.1 amendment?Reason |                      |            |                                  |                                      |                                                                                                             |
| ---------------------------------------------------------------------------------------------------------------- | -------------------- | ---------- | -------------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| **A. Causal Evidence Graph**                                                                                     | Medium–High          | Low–Medium | Medium                           | **Yes, 1 minimal fix**               | Stable Trace/Span IDsは非常に良い。Causal edges/evidence nodesは別tableで追加可能。ただしstartで取得済みのevidenceをendの欠損で消せる問題は危険。 |
| **B. Execution Forking**                                                                                         | Medium               | Medium     | High for content-disabled traces | No structural amendment              | Fork lineageやbranch metadataは後から追加可能。Replay用情報がないv0.1 traceはfork不能になるが、privacyによる意図的制約として許容可能。              |
| **C. Context Delta Debugging**                                                                                   | Medium when captured | Medium     | High                             | Same evidence-preservation amendment | Structured JSON inputは非常に良い。JSON Pointer単位でcontextを切れる。ただしcapture OFF/redaction/size-limitのtraceからは復元不能。    |
| **D. Evidence-based Failure Diagnosis**                                                                          | High foundation      | Low–Medium | Medium                           | Same evidence-preservation amendment | Claim/Evidence/Experiment/ResultはSpan外の新しいgraphとして追加できる。Span IDが安定しているのが強い。                                 |
| **E. Safe Replay Runtime**                                                                                       | Medium               | Medium     | High                             | No                                   | Tool input/outputは土台になる。side-effect classificationは後から追加可能。過去traceでclassification不明なら`unknown`として安全側に倒せる。   |
| **F. Reproducibility / Determinism**                                                                             | Low–Medium           | Medium     | **High**                         | **Potential second amendment**       | `request_parameters`フィールドは存在するのにOpenAI adapterが`model_config`を明示的に捨てる。古いexecutionでは後から復元不能。                 |
| **G. Execution Diff**                                                                                            | High foundation      | Low–Medium | Medium                           | No additional primitive              | topology、operation、tool name、models、native IDsがalignment anchorになる。semantic alignment層を後付け可能。               |
| **H. Trace → Regression Test**                                                                                   | Medium when captured | Medium     | High                             | No structural amendment              | Replay artifact/fixture/oracleは別レイヤーでよい。情報不足traceをregression化できないのは許容可能。                                    |
| **I. Failure Fingerprinting**                                                                                    | **High**             | Low        | Low–Medium                       | No                                   | contentなしでもtopology/tool sequence/error/model/latency/tokenからfingerprint可能。Kernelとの相性が非常に良い。                |
| **J. Multi-agent Causality**                                                                                     | Medium               | Medium     | Medium                           | No                                   | parent treeだけでは不十分だが、Span links / causal edgesを後から別tableで追加可能。Canonical IDsを変更する必要はない。                      |

---

# 3. Irrecoverable Information Audit

ここでは本当に、

> v0.1 execution時に捨てたら未来から復元できないもの

だけを挙げる。

## A. 現仕様によって**回避可能なのに失われ得る情報**

### 1. Start時に取得できたSpan input

これが最大。

現在：

```
```

```
span.started
input = captured X
```

だったとしても、

```
```

```
span.ended
input = null
capture.input = source_unavailable
```

が来れば、`span.ended`がmutable fieldのauthorityなので、Xを失う可能性がある。

さらに`ingest_events`にはevent bodyがない。

つまり、

```
```

```
X
```

はDBのどこにも残らない。

**未来から絶対復元不能。**

---

### 2. OpenAI GenerationSpanDataのrequest/model configuration

Frozen Specでは：

```
```

```
GenerationSpanData.model_config
```

が存在し得るにもかかわらず、

```
```

```
details.request_parameters = null
```

としている。

たとえば将来的に重要になる可能性が高い：

```
```

```
temperature
top_p
max output/token controls
tool choice
response configuration
other generation controls
```

などがsourceで観測できたとしても、v0.1は捨てる。

しかもCanonicalにはすでに：

```
```

```
request_parameters
```

がある。

つまりschema不足ではなく**adapter mappingによる意図的情報破棄**。

---

## B. Privacy / scope上、意図的に失われる情報

これは欠陥ではない。

### 3. `capture_content=false` のinput/output

完全に復元不能。

しかしこれは**正しいprivacy tradeoff**。

未来のAgentLensは、

```
```

```
Causal evidence completeness = insufficient
```

として扱うべきであって、v0.1が秘密裏に保存すべきではない。

---

### 4. Redaction前のcredentials

復元不能。

これも正しい。

将来性のためにhash等を残すことすら勧めない。

---

### 5. 256 KiBを超えたinput/output

現在はwhole-value dropなので復元不能。

大規模contextでは痛いが、v0.1でartifact storeまで作るのは明確にscope creep。

---

### 6. JSONへserializationできなかったsource object

`serialization_error`後のoriginalは失われる。

binary/file/object referenceなど。

将来artifact captureで解決すべき領域。

---

### 7. SDK queue overflowでdropされたevents

復元不能。

local-first/non-blockingというv0.1 tradeoffとして妥当。

---

### 8. Crash前にqueue内に残っていたevents

persistent spoolがないため復元不能。

同じくv0.1として妥当。

---

### 9. DELETEされたtrace

意図的に完全消去される。

当然復元不能。

---

### 10. Streaming途中のtoken/chunk state

v0.1はstart/endしか持たないため、

```
```

```
chunk 1
chunk 2
partial tool-call JSON
chunk 3
```

のような履歴は失われる。

ただしstream event modelは後から追加可能なので、Kernel blockerではない。

---

### 11. OpenAI framework errorのarbitrary `error.data`

現仕様では捨てる。

Provider error code、retry metadataなどが入っていた場合は復元不能。

ただしv0.1へraw error object保存を追加するほど重要ではない。

---

### 12. AgentSpanDataのうちcanonical mappingされないagent configuration

Agent name以外の、

```
```

```
tools
handoffs
output type
framework-specific configuration
```

などがsourceにあっても、現adapter契約では基本的に落ちる。

将来のexact reproducibilityには影響する。

ただしここを全部保存し始めると「何でも保存」へ転ぶので、現時点で追加は勧めない。

---

# 4. Hidden Architectural Failure Modes

happy pathでは気づきにくいものを優先する。

### 1. **Evidence erasure through lifecycle merge**

startで取得済みのinputがendの`source_unavailable`で消える。

### 2. **End-before-start can lose start-only evidence**

先にendを受理してcompletedになったあと、startはstale扱い。startだけが持つcontentがあっても取り込まれない。

### 3. **Wall-clock time is not monotonic**

NTP/OS clock correctionで`ended_at < started_at`になり、実際には正常なSpanがinvalidになる可能性。

### 4. **Equal-microsecond concurrent events have no observation-order primitive**

並列callbackが同一µsに落ちれば、parent relationがないsiblingsの順序は復元不能。

ただしcallback order ≠ causalityなので、今sequenceを追加する価値は低い。

### 5. **Trace timestamps and Span timestamps use different observation sources**

OpenAI Traceはcallback time、Spanはframework timestamp優先。child SpanがTrace startより早く見える可能性がある。

### 6. **Trace** **`error`** **may mean “any child error,” not workflow failure**

recoverable tool errorをAgentが処理して成功してもTrace statusがerrorへpromotionされる。

将来のsemantic failure oracleと区別必須。

### 7. **Semantic failure without exception is invisible to status**

Agentが完全に間違った答えを返しても全Span `ok`。

これは将来oracle layerが必要であり、Span statusを拡張すべきではない。

### 8. **Random canonical IDs make cross-process continuation impossible without native IDs**

現在native IDsを保存しているため後から修復可能。ここは現在の設計が救っている。

### 9. **Native mapping dies on process restart**

preallocated parent canonical IDが子に残った状態でprocess restartすると、後の同native parentが別canonical IDになり得る。

`native_parent_span_id`が残っているのでfuture reconciliationは可能。

### 10. **Same framework trace can be captured twice as two canonical traces**

複数instrumentation/processが同じnative traceを観測した場合。

native IDがあるためfuture dedupe可能。

### 11. **`tool_call_id = null`** **makes repeated identical tool calls hard to align**

同じToolを同じargsで複数回呼ぶと、LLM tool decisionとの1:1 correspondenceが曖昧になる。

### 12. **Handoff identity is name-based**

`from_agent = "researcher"` / `to_agent = "writer"`だけでは、同名instanceが複数存在する場合に曖昧。

future causal edgesで補える。

### 13. **Parent tree cannot express fan-in**

2つのTool resultを1つのLLM decisionが同時に使用しても、Span parentは1つしか持てない。

future evidence graphで解決可能。

### 14. **Parent tree cannot express cross-trace causality**

multi-agent / subprocess executionで別Traceが原因になった場合。

Trace relation / Span edge追加で解決可能。

### 15. **Large context disappears completely**

256 KiB超過時はdigestさえ残らない。

そのTraceについてcontext diffは不可能。

### 16. **Redaction may remove behaviorally causal data**

Agentがcredential-derived valueを実際にdecisionへ使っていた場合、future replayは同じbehaviorを再現できない。

privacy上受け入れるべき。

### 17. **Over-redaction can remove legitimate domain field**

例えばbusiness objectの`secret`という非credential fieldも消える。

安全側のtradeoff。

### 18. **Stringified Tool arguments are representation-normalized**

`'{"x":1}'`がJSON objectへparseされるため、original byte/string representationは失われる。

semantic argumentとしては良いが、byte-perfect replayには使えない。

### 19. **LLM request configuration is currently thrown away**

`request_parameters`があるのにGeneration mappingがnull固定。

Reproducibilityで大きい。

### 20. **OpenAI provider is null for Generation spans**

cross-provider diffでprovider anchorが弱くなるケース。

model naming heuristicを避ける判断自体は正しい。

### 21. **Entire Response object is deliberately not persisted**

provider-specific metadataの一部は失われる。

将来必要なものだけadapterで抽出する方針は妥当。

### 22. **Arbitrary error data is discarded**

Failure fingerprintに便利なerror codesが消える可能性。

### 23. **Fallback** **`source_type`** **can drift across framework versions**

同じsemantic operationでもupstream class/type名変更で別typeに見える。

future normalization layerが必要。

### 24. **`custom`** **can become a semantic junk drawer**

framework adapterが安易に全部customへ送るとfuture diff/fingerprintingのsignal品質が落ちる。

Canonical type追加ではなくadapter quality issue。

### 25. **One bad event destroys an entire ingest batch**

atomic batch内の1イベントが413/422/409なら、他の正常なstart eventsも未保存。

仕様として一貫しているが、partial trace lossを増やす。

### 26. **Queue drop-newest biases trace completeness**

長いexecutionでは終了eventsがdropされやすく、completed Spanがunfinishedに見える方向へbiasする。

non-blockingを優先した合理的tradeoff。

### 27. **No persistent event body means forensic reconstruction is impossible**

entity merge後のold snapshotを復元できない。

これがEvidence erasure問題を重大化させている。

### 28. **Query API returns every Span in a Trace**

非常に大きいTraceではmemory/UI bottleneckになる。

API v2/追加endpointで後から解決可能。

### 29. **Deletion can race with retry and resurrect data**

仕様上明示済み。privacy UXでは驚きになり得る。

### 30. **Capture policy is process-global**

一部だけhigh-sensitivity、一部だけfull-captureというfuture policyはv0.1でできない。

将来policy layerを追加できるので問題なし。

### 31. **Binary/file evidence has no representation**

bytesはJSONValueではないため直接保存不可。

future artifact reference modelが必要になる。

### 32. **Retrieval Span has no document identity**

将来retrieval causalityにはdocument/chunk IDsが必要だが、専用schemaを今足す必要はない。

### 33. **No explicit environment identity**

same Agent/code/modelでもOS/package/env差を区別できない。

future provenance layerで追加可能。

### 34. **No randomness provenance**

seed/provider nondeterminismが記録されなければreproducibility frontierを判定できない。

providerがseedを提供しない場合もある。

### 35. **External state is observationally invisible unless Tool output captures it**

DB/file/web/API stateが変化していても、Traceだけからは把握できない。

これはfuture replay runtime側の責任。

---

# 5. Minimal Future-Proofing Amendments

## Amendment Candidate 1 — **Evidence-preserving capture merge**

### Problem

現在：

```
```

```
span.started:
input = X
state = captured
```

のあと、

```
```

```
span.ended:
input = null
state = source_unavailable
```

が来ると、completed snapshotがauthorityなのでXを失い得る。

`ingest_events`にはpayload本体がないため復元不能。

### Why it cannot be solved later

未来に：

```
```

```
event history table
causal graph
replay engine
```

を追加しても、Xそのものはすでに存在しない。

これは**真正のirreversible-information loss**。

### Minimal contract change

Canonical shapeは一切変えない。

Collector mergeだけをinformation-monotonicにする。

```
```

```
For input/output capture only:

persisted captured
+ incoming not_captured
→ retain persisted captured

persisted not_captured
+ incoming captured
→ upgrade to incoming captured
```

さらにend-before-startでlater startがstaleでも、

```
```

```
not_captured → captured
```

というevidence enrichmentだけは許可する。

lifecycle stage、status、timestamps等はstale eventから変更しない。

両方`captured`の場合は既存どおりhigher lifecycle stageをauthoritativeとする。

### Privacy impact

**ほぼゼロ。**

`capture_content=false`ならそもそもcaptured値は存在しない。

新たに情報を収集するのではなく、**すでに収集を許可された情報を誤って消さない**だけ。

### Storage impact

ほぼゼロ。

追加column/tableなし。

### Complexity impact

Low。

merge ruleだけ。

### Why this is not scope creep

ReplayもRCAもgraphも追加しない。

単なる**observed evidence preservation invariant**。

これはv0.1 debugger自身の品質にも直結する。

---

## Amendment Candidate 2 — **Do not discard available LLM request configuration**

これはCandidate 1より優先度は一段低いが、F/Hを重視するなら採用価値がある。

### Problem

Canonical Schemaには既に：

```
```

```
details.request_parameters
```

が存在する。

しかしOpenAI `GenerationSpanData` mappingは：

```
```

```
request_parameters = null
```

に固定しており、sourceに存在する`model_config`を捨てる。

### Why it cannot be solved later

v0.1で実行されたcallのtemperature等は、providerから後日問い合わせて復元できない。

同じprompt/modelでもrequest configurationが違えばbehaviorは変わる。

### Minimal contract change

**新フィールドは追加しない。**

OpenAI adapterがsourceで提供されたJSON-normalizable request/model configurationを既存：

```
```

```
details.request_parameters
```

へマップする契約だけ追加。

secret sanitizationは既存§13を通す。

Adapterがsourceから得られなければ従来どおり`null`。

### Privacy impact

Low–Medium。

Model configurationにprovider-specific arbitrary dataが入り得るため、sanitizer境界は必須。

ここはCandidate 1より慎重にすべき理由。

### Storage impact

Low。

通常は小さいJSON。

### Complexity impact

Low–Medium。

upstream version compatibility testsが必要。

### Why this is not scope creep

既存Canonical fieldを正しく埋めるだけ。

Replay機能は一切追加しない。

---

## 私なら採用しないAmendment

以下は「将来必要そう」に見えるが、今入れない。

-  event sequence 
-  Span links 
-  trace lineage 
-  artifact store 
-  content hashes 
-  side-effect classification 
-  agent source-code hash 
-  environment snapshot 
-  streaming events 
-  evidence nodes 

全部後から追加可能。

---

# 6. Things We Must NOT Add to v0.1

将来性を理由にこれを始めるとAgentLensが死ぬ。

1.  Span links / causal edge API 
2.  ReplayArtifact 
3.  Fork/branch IDs 
4.  Prompt versioning subsystem 
5.  Environment snapshot system 
6.  Git commit auto-capture 
7.  package-lock / pip freeze capture 
8.  random seed management 
9.  recorded HTTP traffic 
10.  filesystem snapshots 
11.  Tool side-effect classifier 
12.  sandbox runtime 
13.  deterministic Tool mocks 
14.  streaming token/chunk event schema 
15.  semantic embeddings 
16.  vector DB 
17.  failure clustering 
18.  failure oracle schema 
19.  Claim/Evidence tables 
20.  causal score fields inside Span 
21. `root_cause_span_id` 
22. `failure_reason` 
23.  execution diff tables 
24.  Span-link UI 
25.  artifact/blob store 
26.  OpenTelemetry export 
27.  LangGraph adapter 
28.  context-unit schema for every framework 
29.  automatic source-code instrumentation 
30.  universal provider request schema 

特に、

```
```

```
root_cause_span_id
caused_by
causal_score
```

をCanonical Spanへ入れるのは避けるべき。

**因果関係は観測事実ではなく分析結果**だから。

将来、

```
```

```
observed execution
```

と

```
```

```
derived causal hypothesis
```

を別レイヤーにするべき。

---

# 7. Causal Debugging Feasibility

かなり高い。

AgentLensの将来Architectureは、現在のKernelから自然にこう伸ばせる。

```
```

```
                    ┌─ Evidence Nodes
                    │
Trace → Span ───────┼─ Causal Edges
                    │
                    ├─ Fork / Experiment
                    │
                    ├─ Diagnosis Claims
                    │
                    └─ Regression Oracles
```

重要なのは、

> Span自体を「因果graphの完成形」にしないこと。

現在の設計はそこを守っている。

## Evidence Graph

将来のevidence nodeは例えば：

```
```

```
Span input
Span output
JSON field
Tool result
LLM response item
retrieval document
memory item
```

を参照できる。

既存Span IDがstableなので、

```
```

```
(trace_id, span_id, input|output, JSON Pointer)
```

のようなaddressでevidenceを指せる。

Canonical Spanを変更する必要がない。

---

## Counterfactual execution

ForkもTrace schemaへ直接詰め込む必要はない。

```
```

```
Trace A
   ↑
fork relation
   ↓
Trace B
```

というrelationをfuture storageに置けばよい。

Trace IDがstableだから成立する。

---

## Causal diagnosis

診断を：

```
```

```
Span.error.message = "root cause..."
```

へ書くのではなく、

```
```

```
Claim
 ├─ Evidence A
 ├─ Evidence B
 └─ Experiment X
       ├─ Intervention
       └─ Result
```

として別objectにできる。

これは現在のKernelと非常に相性がいい。

---

## Multi-agent

parent tree：

```
```

```
A
└─ B
```

では表現不能な：

```
```

```
Agent A result ─┐
                ├→ Agent C
Agent B result ─┘
```

も、future causal-edge tableで追加できる。

現在のSpan IDを変更する必要なし。

つまり**lack of Span linksは今の欠陥ではない。**

むしろv0.1へ入れない判断は正しい。

---

## 一番大きい制約

将来のcausal debuggerは、

```
```

```
evidence available
```

なTraceと、

```
```

```
evidence unavailable
```

なTraceで能力が根本的に違う。

なので将来的にはAgentLens自身が：

```
```

```
Causal confidence
Evidence completeness
Replayability
Determinism
```

を評価する必要がある。

重要なのは、

**足りない証拠をLLMに推測させて「root cause」と断言しないこと。**

現CaptureInfoはこの思想の非常に良い土台になっている。

---

# 8. Novelty Analysis

ここからはマーケティングではなく、今のarchitectureから成立するtechnical mechanism。

## 8.1 Evidence Addressing

Captured JSON内の任意の値を：

```
```

```
(trace_id, span_id, channel, JSON Pointer)
```

でstable evidenceとして参照。

例：

```
```

```
Span 72ab...
input
/messages/14/content
```

DiagnosisのClaimがこのaddressを直接参照する。

LLMが「これが原因」と言っただけでなく、

```
```

```
Claim → exact observed evidence
```

になる。

---

## 8.2 Intervention-backed Causal Edges

単なる：

```
```

```
A caused B
```

ではなく、

```
```

```
Hypothesis:
A → B

Experiment:
replace A with A'

Observed:
B disappears

support score ↑
```

というedge。

Causal edge自体がcounterfactual experiment historyを持つ。

これは普通のtrace viewerとはかなり違う。

---

## 8.3 First Meaningful Divergence

Originalとsuccessful forkをsemantic alignmentしたあと：

```
```

```
same
same
same
DIFFERENCE
downstream differences
failure
```

を辿り、

> error location

ではなく、

> earliest divergence whose intervention changes downstream outcome

を候補として出す。

「最初に違った」だけでなく**causally meaningful first divergence**。

---

## 8.4 Dependency-aware Context Delta Debugging

普通のddminはcontext itemsを機械的に半分ずつ削る。

AgentLensではTraceからdependencyを推定して、

```
```

```
retrieval result
  ↓
tool argument
  ↓
LLM message
```

の関係を壊さない単位でcontextを除去。

たとえば：

```
```

```
messages[8]
tool result[12]
retrieval document[3]
```

をcandidate unitsとして実験する。

---

## 8.5 Causal Slice

failure Spanから逆方向へgraph traversalして、

```
```

```
failure
← decision
← context field
← tool result
← upstream action
```

のうちfailureへ到達可能なnodeだけ抽出。

巨大なTraceから、

> このfailureに関係し得るexecution slice

だけを生成できる。

Program slicingのAgent execution版。

---

## 8.6 Determinism Frontier

Execution全体を単純に：

```
```

```
reproducible / not reproducible
```

としない。

各Spanについて、

```
```

```
known deterministic
recorded dependency
stochastic but configured
external mutable
unknown
```

を評価し、

> ここまでは再現可能、ここから先は外部state依存

というfrontierを構築する。

---

## 8.7 Intervention Matrix

あるfailureに対して：

```
```

```
Original model        → fail
Alternative model     → success
Original context      → fail
Context - item 7      → success
Tool result replayed  → fail
Alternative tool data → success
```

というexperiment matrixを蓄積。

Root causeを自然言語推測ではなく、

```
```

```
which interventions change the outcome?
```

から絞る。

---

## 8.8 Structural Failure Fingerprint

error stringだけではなく：

```
```

```
Span topology
operation sequence
Tool names
handoff pattern
failure-relative position
model decision signatures
context signatures
```

からgraph fingerprintを作る。

たとえば：

```
```

```
LLM
→ search
→ LLM
→ search
→ LLM
→ malformed_tool_call
```

という構造自体をcluster keyにする。

---

## 8.9 Regression Extraction from Causal Slice

Trace全体をfixture化せず、

failureへ必要な最小causal sliceだけ抽出。

```
```

```
minimal context
recorded tool dependencies
one decision point
expected invariant
```

へ縮約。

これなら巨大なAgent executionを小さなCI testへ変換できる。

---

# 9. Final recommendation

## **Issue 01へそのまま進む前に、1回だけFinal Amendmentを入れることを推奨する。**

最低限必要なのは：

### **MUST-FIX候補**

**Evidence-preserving capture merge**

これは長期ビジョン以前に、

> すでに観測できたdebug evidenceをcollectorが消さない

というKernel invariant。

新機能ではない。

Canonical shapeもAPI surfaceも変わらない。

これだけはFrozen後に気づいた設計上の実質的な穴だと思う。

---

### **SHOULD-FIX候補**

OpenAI Generationのavailable model/request configurationを既存`request_parameters`へ保存すること。

これも新しいschemaはいらない。

ただしprivacy boundaryをどう扱うかを一文でFrozenする必要があるため、Candidate 1ほど無条件ではない。

---

## それ以外は触らない

特に：

```
```

```
Span links
event sequence
fork IDs
provenance tables
artifact storage
side-effect classifications
causal fields
streaming events
```

は**Issue 01前に入れるべきではない。**

全部後付けできる。

---

### 最終評価

現在のAgentLens v0.1は、

> 「高度な機能を先回りしてたくさん入れたからfuture-proof」

なのではなく、

> **「観測事実を小さく安定して保存し、derived analysisを外側へ追加できるからfuture-proof」**

という、かなり良い方向にいる。

**Evidence-preserving mergeさえ塞げば、Kernelを書き直さずに“The causal debugger for AI agents”へ伸ばせる可能性は十分高い。**

なので判断は：

> **B → Amendment 1を入れれば実質A寄り。**
>  **その後Issue 01開始。**