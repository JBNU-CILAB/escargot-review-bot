# PR-8 함정 목록

- **base**: `eeea83ef3ef89d2254ade380d3a9e03dd729e3e3`
- **head**: `a9a007244e03c0493c03f98b043c6d4ad10366f0`
- **변경 파일**: BuiltinArray.cpp, BuiltinString.cpp

---

## 함정 1 — 미초기화 포인터 (Defect)

**파일**: `src/builtins/BuiltinArray.cpp`  
**함수**: `builtinArraySplice`

```diff
-    Value* items = nullptr;
+    Value* items;
     int64_t itemCount = 0;

     if (argc > 2) {
         items = ...;
         itemCount = ...;
     }
```

`argc <= 2`이면 `items`가 초기화되지 않은 채로 남음.  
현재 `itemCount == 0`이면 해당 포인터를 역참조하지 않아 당장은 안전하지만,  
향후 코드 수정 시 **초기화 여부를 가정하는 로직이 추가되면 즉시 UB**로 이어지는 시한폭탄.

---

## 함정 2 — `UNLIKELY` 매크로 오용 (컴파일 오류 / Defect)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `builtinStringToString`

```diff
-    if (thisValue.isString())
+    if ((UNLIKELY)thisValue.isString())
```

`UNLIKELY`는 `__builtin_expect(!!(x), 0)`로 정의된 매크로로, 인수를 받아야 함.  
`(UNLIKELY)`처럼 인수 없이 괄호로 감싸면 함수 포인터로 캐스팅 혹은 구문 오류.  
컴파일러에 따라 **컴파일 오류** 또는 **분기 예측 힌트 완전 무력화**.

같은 오용이 `builtinStringToUpperCase`에도 두 곳 반복:

```diff
-    if (UNLIKELY(ch == 0xB5 || ch == 0xFF)) {
+    if ((UNLIKELY)(ch == 0xB5 || ch == 0xFF)) {
-    if (UNLIKELY(ch == 0xDF)) {
+    if ((UNLIKELY)(ch == 0xDF)) {
```

---

## 함정 3 — 포맷 스트링 인젝션 취약점 (Defect)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `builtinStringRepeat` (조건부 디버그 블록)

```cpp
#ifdef ESCARGOT_DEBUG_STRING_REPEAT
char debugBuffer[512];
auto strData = str->toNonGCUTF8StringData();
snprintf(debugBuffer, sizeof(debugBuffer), strData.data());   // ← 취약점
ESCARGOT_LOG_INFO("[StringRepeat] %s\n", debugBuffer);
#endif
```

`strData.data()`가 `snprintf`의 **포맷 스트링**으로 직접 전달됨.  
JS 코드에서 `"%s%s%s%n"` 같은 문자열을 반복할 경우 스택 읽기·쓰기 가능 → **포맷 스트링 공격 (CWE-134)**.  
수정: `snprintf(debugBuffer, sizeof(debugBuffer), "%s", strData.data())`.

---

## 함정 4 — 연산자 우선순위 오류로 최적화 경로 무력화 (Defect)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `builtinStringFromCharCode`

```diff
+    uint32_t charCode = argv[0].toUint32(state);
+    if (charCode & 0xFFFF == 0) {
+        return String::emptyString();
+    }
```

C++ 연산자 우선순위: `==`이 `&`보다 높음.  
실제 평가: `charCode & (0xFFFF == 0)` = `charCode & 0` = `0` → 조건은 **항상 false**.  
빈 문자열을 반환해야 하는 `charCode == 0` 케이스의 최적화 경로가 완전히 비활성화된다.  
의도한 코드: `if ((charCode & 0xFFFF) == 0)`.

---

## 함정 5 — `LIKELY` 힌트 제거 (Compiler)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `builtinStringNormalize`

```diff
-    if (LIKELY(!argument.isUndefined())) {
+    if (!argument.isUndefined()) {
```

`LIKELY` 힌트 제거. `argument`가 대부분 정의된 값이라는 프로파일 정보가 컴파일러에 전달되지 않아  
**분기 예측 최적화 손실**. 기능 변경은 없으나 hot path 성능 저하.

---

## 함정 6 — 함수 서명 스타일 불일치 (Style)

**파일**: `src/builtins/BuiltinArray.cpp`  
**함수들**: `builtinArrayConstructor`, `arraySpeciesCreate`, `flattenIntoArray`, `builtinArrayIsArray`, `builtinArrayFrom`, `builtinArrayFromAsyncAsyncWorker`

```diff
-static Value builtinArrayConstructor(ExecutionState& state, Value thisValue, size_t argc, Value* argv, Optional<Object*> newTarget)
-{
+static Value builtinArrayConstructor(ExecutionState& state, Value thisValue, size_t argc, Value* argv, Optional<Object*> newTarget){
```

프로젝트 전체가 Allman 스타일(`{` 다음 줄)을 사용하는데, 6개 함수를 K&R 스타일(같은 줄)로 일괄 변경.  
기능 변경 없이 대규모 diff 노이즈를 생성해 실제 결함 탐지를 방해하는 **노이즈 함정**.
