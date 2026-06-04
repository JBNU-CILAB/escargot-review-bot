# PR-7 함정 목록

- **base**: `eeea83ef3ef89d2254ade380d3a9e03dd729e3e3`
- **head**: `1190a3b96332a9a5060542f1c28ec5bf6b93804b`
- **변경 파일**: Escargot.h, BuiltinString.cpp, CodeCacheReaderWriter.cpp, CustomAllocator.h, RegExpObject.cpp, String.cpp

---

## 함정 1 — include guard 변경으로 다중 포함 보호 파괴 (Compiler)

**파일**: `src/Escargot.h`

```diff
-#ifndef __Escargot__
-#define __Escargot__
+#ifndef Escargot
+#define Escargot
```

헤더 가드가 `__Escargot__`에서 `Escargot`으로 바뀜.  
다른 파일이 `#ifdef __Escargot__`로 포함 여부를 검사하고 있다면 **컴파일 오류** 또는 조건 분기 오동작.  
(`__Escargot__`는 이중 언더스코어로 구현 예약 이름이나, 기존 코드베이스가 이를 관례로 사용 중인 경우 파괴적 변경)

---

## 함정 2 — 템플릿 닫기 `>>` → `> >` 구식 스타일 (Style)

**파일**: `src/Escargot.h`

```diff
-using HashMap = tsl::robin_map<Key, T, Hash, KeyEqual, Allocator, StoreHash, GrowthPolicy>;
+using HashMap = tsl::robin_map<Key, T, Hash, KeyEqual, Allocator, StoreHash, GrowthPolicy> >;
```

C++03에서는 `>>`가 우측 시프트 연산자로 파싱되어 `> >`가 필요했으나, C++11 이후 불필요.  
현대 컴파일러에서 기능 차이는 없지만 **의도적인 스타일 퇴행**.

---

## 함정 3 — `return` 이후 도달 불가 코드 (Defect / Style)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `builtinStringIndexOf`

```diff
     if (pos == std::numeric_limits<double>::infinity() || std::isnan(pos)) {
         return Value(-1);
+        pos = 0;          // ← 절대 실행되지 않음
     }
```

`pos = 0;`은 `return` 뒤에 위치하여 실행 불가. 의도가 "NaN은 0으로 처리"였다면 `return` 앞으로 와야 함.  
현재 코드는 `NaN` 입력 시 명세(0 처리 후 탐색)와 달리 즉시 `-1`을 반환한다.

```diff
     if (argc == 0) {
         return str;
+        state.clearException();   // ← 절대 실행되지 않음
     }
```

`clearException()`은 `return` 뒤라 실행되지 않는다. 컴파일러 경고 대상.

---

## 함정 4 — `String.prototype.at()` off-by-one 경계 검사 (Defect)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `builtinStringAt`

```diff
-    if (relativeStart < 0 || relativeStart >= len) {
+    if (relativeStart < 0 || relativeStart > len) {
```

`>=`가 `>`로 변경되어 `relativeStart == len`일 때 경계 검사를 통과.  
이후 `str->charAt(len)` 접근 → **범위 외 문자 읽기 (UB)**.  
ECMAScript 명세상 `at(len)`은 `undefined` 반환이어야 한다.

---

## 함정 5 — 배열 `delete[]` → `delete` (Defect)

**파일**: `src/codecache/CodeCacheReaderWriter.cpp`  
**함수**: `CodeCacheReader::loadStringTable`

```diff
-        delete[] buffer;
+        delete buffer;
         ...
-        delete[] lBuffer;
-        delete[] uBuffer;
+        delete lBuffer;
+        delete uBuffer;
```

`new[]`로 할당된 배열을 `delete`(스칼라)로 해제하면 **undefined behavior**.  
실제로는 힙 손상 또는 단 첫 번째 요소의 소멸자만 호출될 수 있다.

---

## 함정 6 — `explicit` 제거로 암묵적 변환 허용 (Defect)

**파일**: `src/heap/CustomAllocator.h`  
**템플릿**: `CustomAllocator`

```diff
-    explicit CustomAllocator(const CustomAllocator<GC_Tp1>&) noexcept {}
+    CustomAllocator(const CustomAllocator<GC_Tp1>&) noexcept {}
```

`explicit`이 없으면 다른 타입의 `CustomAllocator<T>`를 `CustomAllocator<U>`로 **암묵적 변환**할 수 있게 됨.  
STL 컨테이너의 rebind 과정에서 의도치 않은 생성자 호출 → 타입 안전성 저하.

---

## 함정 7 — 전위 증가 → 후위 증가 (Style / Compiler)

**파일**: `src/runtime/RegExpObject.cpp`  
**함수**: `RegExpObject::createRegExpMatchedArray`

```diff
-    for (auto it = ...; it != end; ++it) {
+    for (auto it = ...; it != end; it++) {
```

이터레이터 후위 증가(`it++`)는 이전 값을 복사한 임시 객체를 생성.  
복잡한 이터레이터에서 **불필요한 복사 오버헤드**. `++it`(전위)가 관례이자 더 효율적.

---

## 함정 8 — `std::move`로 NRVO 억제 (Compiler)

**파일**: `src/runtime/String.cpp`  
**함수**: `utf8StringToUTF16StringNonGC`

```diff
-    return str;
+    return std::move(str);
```

컴파일러는 지역 변수를 반환할 때 NRVO(Named Return Value Optimization)를 적용해 복사를 생략할 수 있음.  
`std::move`를 명시하면 NRVO를 억제하고 **불필요한 이동 생성자 호출**을 강제. 코드 크기·성능 모두 저하.
