# MiroFish 번역 용어집 (zh / en / ko)

| 中文 | English | 한국어 |
|---|---|---|
| 智能体 / Agent | Agent | 에이전트 |
| 模拟 | Simulation | 시뮬레이션 |
| 本体 | Ontology | 온톨로지 (첫 설명에서만 '온톨로지(개념 구조)') |
| 图谱 | Graph / Knowledge graph | 그래프 / 지식 그래프 |
| 图谱构建 | Graph Build | 그래프 구축 |
| 环境搭建 | Env Setup | 환경 설정 |
| 报告 | Report | 보고서 |
| 深度互动 | Deep Interaction | 심층 상호작용 |
| 现实种子 | Reality Seed | 현실 씨앗 |
| 实体 | Entity | 엔티티 |
| 关系 | Relation / Relationship | 관계 |
| 节点 | Node | 노드 |
| 边 | Edge | 엣지 |
| 人设 | Persona | 페르소나 |
| 模拟个体 | Simulated individual | 시뮬레이션 인물 |
| 时序记忆 | Temporal memory | 시간 기반 메모리 |
| 轮次 | Round | 라운드 |
| 双平台 | Dual-platform | 두 플랫폼 |
| 平行世界 | Parallel world | 평행 세계 |
| 上帝视角 | God's-eye view | 전지적 시점 |
| 局部最优解 | Local optimum | 국소 최적해 |
| 群体智能 | Swarm intelligence | 군집 지능 |
| 叙事方向 | Narrative direction | 서사 방향 |
| 热点话题 | Hot topic | 화제 |
| 初始激活 | Initial activation | 초기 활성화 |
| 推荐算法 | Recommendation algorithm | 추천 알고리즘 |
| 回音室 | Echo chamber | 에코 체임버 |
| 情感倾向 | Sentiment bias | 감정 성향 |
| 影响力 | Influence | 영향력 |
| 活跃度 | Activity level | 활동 수준 |
| 高峰时段 | Peak hours | 피크 시간대 |
| 问卷 | Survey | 설문 |
| 采访 | Interview | 인터뷰 |
| 子问题 | Sub-query | 하위 질문 |
| 事实 | Fact | 사실 |
| 活跃/历史事实 | Active / Historical facts | 현재 / 과거 사실 |
| 大纲 | Outline | 개요 |
| 章节 | Section | 섹션 |
| 工具调用 | Tool call | 도구 호출 |
| 文本块 | Chunk | 청크 |
| 批次 | Batch | 배치 |
| 任务 | Task | 작업 |
| 项目 | Project | 프로젝트 |
| 模拟需求 | Simulation requirement | 시뮬레이션 요구사항 |
| 自定义 | Custom | 직접 설정 |
| 工作台 | Workbench | 작업대 |
| 强制停止 | Force stop | 강제 종료 |
| 初始化 | Initialize | 준비 (예: '준비 중…', 'View 준비 완료') |

## 톤 규칙

1. 메시지와 안내문은 해요체로 써요. 예: '그래프 구축이 끝났어요.', '먼저 /prepare를 호출하세요.'
2. 버튼, 탭, 라벨, 단계 이름은 명사형으로 끝내요. 예: '시뮬레이션 시작', '보고서 생성', '설문 보내기'.
3. 진행 상태는 '~하는 중…' 형태를 쓰고, 말줄임은 '…' 한 글자를 써요.
4. 실패는 '~하지 못했어요: {error}'로, 예외는 '~ 중 오류가 생겼어요: {error}'로 통일해요. 로그의 ✓/✗ 줄은 짧은 명사형도 괜찮아요.
5. 사용자가 아는 기술 용어(에이전트, 시뮬레이션, 엔티티, GraphRAG, LLM)는 그대로 써요. 도구 이름(InsightForge, ReportAgent, Zep)과 브랜드 MiroFish는 번역하지 않아요.
6. API 파라미터, 파일 이름, 경로(project_id, /api/..., section_{index}.md)는 원문 그대로 둬요.
7. {placeholder} 토큰은 그대로 유지하고 어순만 한국어에 맞게 옮겨요. 숫자 뒤 단위는 붙여 써요. 예: '{count}개', '{hours}시간'.
8. '~를 진행하다', '~에 대한', '~되어지다' 같은 번역투를 피하고 짧게 끊어요.
