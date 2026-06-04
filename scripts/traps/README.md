# Escargot 리뷰 봇 — 실험용 함정 PR 목록

리뷰 봇의 탐지 능력을 평가하기 위해 `Samsung/escargot`에 의도적으로 심은 결함 목록입니다.  
모든 PR은 공통 base `eeea83ef3ef89d2254ade380d3a9e03dd729e3e3` 기준입니다.

| PR | head SHA | 함정 수 | 주요 유형 |
|----|----------|---------|-----------|
| [PR-4](PR-4.md) | `41c6719` | 6 | Defect (UB·무한루프·OOB), Compiler |
| [PR-5](PR-5.md) | `22f5de0` | 6 | Defect (소멸자·버퍼·누수), Refactor |
| [PR-6](PR-6.md) | `0c997db` | 10 | Defect (off-by-one 집중), Compiler, Style |
| [PR-7](PR-7.md) | `1190a3b` | 9 | Defect (delete[]/댕글링), Compiler, Style |
| [PR-8](PR-8.md) | `a9a007` | 7 | Defect (연산자우선순위·포맷스트링), Compiler |

## 함정 유형 분류

| 유형 | 설명 |
|------|------|
| **Defect** | 런타임 버그 — 크래시, 무한 루프, 메모리 손상, UB, 잘못된 동작 |
| **Compiler** | 컴파일 경고 / 성능 저하 — 최적화 방해, 불필요한 할당, 타입 불일치 |
| **Refactor** | 설계 결함 — 의도치 않은 상속 허용, 암묵적 변환, 오버로드 모호성 |
| **Style** | 관례·가독성 이탈 — 코딩 스타일 퇴행, 도달 불가 코드 |
