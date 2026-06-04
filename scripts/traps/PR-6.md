# PR-6 함정 목록

- **base**: `eeea83ef3ef89d2254ade380d3a9e03dd729e3e3`
- **head**: `0c997db31004765fc4d68675cab5aac4c2ff5b70`
- **변경 파일**: BuiltinString.cpp, CodeCacheReaderWriter.cpp, CodeCacheReaderWriter.h, Lexer.h

---

## 함정 1 — 정규화 버퍼 off-by-one (Defect)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `builtinStringNormalize`

```diff
-    ret.resizeWithUninitializedValues(normalizedStringLength);
+    ret.resizeWithUninitializedValues(normalizedStringLength - 1);
```
`unorm2_normalize`가 `normalizedStringLength` 개의 UTF-16 코드 유닛을 기록하지만 버퍼는 1 적게 할당.  
**힙 버퍼 오버플로우** — `String.prototype.normalize()` 호출 시 발생.

```diff
-    char16_t* src = ALLOCA(len * 2, char16_t);
+    char16_t* src = ALLOCA(len * 2 - 2, char16_t);
```
`len * 2` 개의 `char16_t`가 필요한데 `len * 2 - 2` 개만 할당.  
문자열 마지막 문자 복사 시 스택 버퍼 끝을 1 `char16_t` 초과 → **스택 오버플로우**.

```diff
-    dest = ALLOCA(len, LChar);
+    dest = ALLOCA(len-1, LChar);
```

`len` 바이트가 필요한 변환 버퍼를 `len - 1`로 할당.  
마지막 `LChar` 위치에 쓸 때 스택 오버플로우.

```diff
-    newStr.resizeWithUninitializedValues(len);
+    newStr.resizeWithUninitializedValues(len - 1);
```

`len` 개의 `LChar`가 필요한 출력 버퍼를 `len - 1`로 축소.  
이후 인덱스 `len - 1`에 쓸 때 **버퍼 오버런**.

---

## 함정 2 — 로컬 변수에 대한 댕글링 참조 반환 (Defect)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `getCharAtHelper` (신규 추가)

```cpp
static const String*& getCharAtHelper(ExecutionState& state, String* str, size_t position) {
    const String* result = state.context()->staticStrings().charCodeToString(str->charAt(position));
    return result;   // ← 로컬 변수 참조 반환
}
```

`result`는 함수 스택에 있는 지역 변수. 참조로 반환하면 함수 종료 후 해당 주소는 유효하지 않아  
**댕글링 참조 역참조 (UB / 크래시)**.  
호출처 `builtinStringCharAt`에서 이 반환값을 `Value` 초기화에 사용한다.

---

## 함정 3 — `ASSERT`에서 `==` → `=` (대입) (Defect)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `builtinStringToUpperCase`

```diff
-    ASSERT(destIndex == len + sharpSCount);
+    ASSERT(destIndex = len + sharpSCount);
```

`==`(동등 비교)가 `=`(대입)으로 교체. `destIndex`에 `len + sharpSCount` 값이 대입되고,  
대입 결과(비 0)가 항상 `true`로 평가되어 **어서션이 항상 통과** → 루프 로직 버그가 있어도 탐지 불가.

---

## 함정 4 — `StringBuilder` 불필요한 힙 할당 (Compiler)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `createHTML`

```diff
-    StringBuilder sb;
+    StringBuilder* sb = new StringBuilder();
     ...
-    String* p1 = sb.finalize(&state);
+    String* p1 = sb->finalize(&state);
+    delete sb;
```

`StringBuilder`를 스택이 아닌 힙에 4번 할당·해제.  
기능은 동일하지만 `new/delete` 반복으로 **불필요한 할당 오버헤드** 발생. 예외 발생 시 누수 위험도 생긴다.

---

## 함정 5 — `size_t` → `int` 타입 다운그레이드 (Compiler)

**파일**: `src/codecache/CodeCacheReaderWriter.cpp`  
**함수**: `CacheStringTable::add`

```diff
-    size_t index = 0;
+    int index = 0;
     for (; index < m_table.size(); index++) {
```

`m_table.size()`는 `size_t`(unsigned)이고, `index`는 이제 `int`(signed).  
**signed/unsigned 비교 경고** 발생. 테이블 크기가 `INT_MAX`를 초과하면 오버플로우 후 반환값 오류.

---

## 함정 6 — 클래스 들여쓰기 스타일 변경 (Style)

**파일**: `src/codecache/CodeCacheReaderWriter.h`  
**클래스**: `CacheStringTable`, `CodeCacheWriter`

전체 클래스 멤버를 4스페이스 추가 들여쓰기로 재포맷. `public:` → `    public:` 등.  
기능 변경 없이 파일 전체를 diff로 오염시켜 실제 변경 탐지를 어렵게 만드는 **노이즈 함정**.

---

## 함정 7 — 생성자 한 줄 압축 (Style)

**파일**: `src/parser/Lexer.h`  
**클래스**: `ScannerResult`

```diff
-    ScannerResult()
-        : type(InvalidToken)
-        , ...
-    {
-    }
+    ScannerResult() : type(InvalidToken), ..., valueRegExp() {}
```

13개 초기화 목록을 한 줄로 압축. 가독성 저하 외 기능 변경 없음.
