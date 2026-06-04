# PR-5 함정 목록

- **base**: `eeea83ef3ef89d2254ade380d3a9e03dd729e3e3`
- **head**: `22f5de0651d559471bcb443be259c99bd8af2de9`
- **변경 파일**: BuiltinArray.cpp, BuiltinDate.cpp, BuiltinString.cpp, BuiltinTypedArray.cpp, ErrorObject.h, Platform.h

---

## 함정 1 — `else` 제거로 연도 설정 실패 (Defect)

**파일**: `src/builtins/BuiltinDate.cpp`  
**함수**: `builtinDateSetYear`

```diff
     if (0 <= yAsInteger && yAsInteger <= 99) {
         yyyy = 1900 + yAsInteger;
-    } else {
-        yyyy = y;
-    }
+    }
```

`yAsInteger > 99` (예: 2024)인 경우 `else` 분기가 삭제되어 `yyyy`에 정상 값이 대입되지 않음.  
`Date.prototype.setYear(2024)` 호출 시 `yyyy`는 초기화되지 않은 채로 `setTimeValue()`에 전달 → **잘못된 날짜 설정 (UB)**.

---

## 함정 2 — `malloc` 크기 오류 + ALLOCA 대체 (Defect)

**파일**: `src/builtins/BuiltinString.cpp`  
**함수**: `stringToLocaleConvertCase`

```diff
-    char16_t* src = ALLOCA(len * 2, char16_t);   // len * 2 * sizeof(char16_t) = len * 4 bytes
+    char16_t* dest = (char16_t*)malloc(dest_length);   // dest_length bytes (절반 크기)
```

원래 `ALLOCA(dest_length * 2, char16_t)`는 `dest_length * 2 * sizeof(char16_t)` = `dest_length * 4` 바이트를 할당.  
`malloc(dest_length)`는 `dest_length` 바이트만 할당 — **필요 크기의 1/4**.  
이후 `u_strToUpper` / `u_strToLower`가 full-size 버퍼를 가정하고 쓰기 → **힙 버퍼 오버플로우**.

덧붙여, `free(dest)` 후 `UTF16String` 생성자가 포인터를 복사하지 않는다면 **use-after-free** 추가 발생.

---

## 함정 3 — 스택 변수 대신 `malloc` + 예외 경로 누수 (Defect)

**파일**: `src/builtins/BuiltinTypedArray.cpp`  
**함수**: `builtinTypedArrayToLocaleString`

```diff
-    Value* toLocaleStringArgv = ALLOCA(sizeof(Value) * argc, Value);
+    Value* toLocaleStringArgv = (Value*)malloc(sizeof(Value) * argc);
     while (k < len) { ... }
+    free(toLocaleStringArgv);
```

`while` 루프 내부에서 JS 예외가 발생하면 `free(toLocaleStringArgv)`에 도달하지 못해 **메모리 누수**.  
`ALLOCA`는 스택 해제로 자동 정리되나, `malloc` 전환 후 예외 안전성이 사라졌다.

---

## 함정 4 — `virtual` 소멸자 제거 (Defect)

**파일**: `src/runtime/Platform.h`  
**클래스**: `Platform`

```diff
-    virtual ~Platform() {}
+    ~Platform() {}
```

`Platform`은 순수 가상 함수를 가진 추상 클래스. 소멸자의 `virtual`이 제거되면  
`Platform*` 포인터로 파생 클래스 객체를 삭제할 때 **파생 클래스 소멸자가 호출되지 않음** → 자원 누수 및 UB.  
엔진 수명 주기 전반에 영향을 주는 심각한 결함이다.

---

## 함정 5 — 생성자 추가로 인한 오버로드 모호성 (Refactor)

**파일**: `src/runtime/ErrorObject.h`  
**클래스**: `ReferenceErrorObject`

```diff
+    ReferenceErrorObject(ExecutionState& state, String* errorMessage)
+        : ReferenceErrorObject(state, nullptr, errorMessage, true, false) {}
     ReferenceErrorObject(ExecutionState& state, Object* proto, String* errorMessage,
                          bool fillStackInfo = true, bool triggerCallback = false);
```

기존 생성자의 `proto`, `fillStackInfo`, `triggerCallback`에 기본값이 있어,  
`ReferenceErrorObject(state, str)` 호출 시 새 2-인수 생성자와 기존 5-인수(기본값 포함) 생성자 간 **모호한 오버로드** 발생 가능.  
의도와 다른 생성자가 선택될 위험이 있다.

---

## 함정 6 (Style) — 단일행 주석 → 블록 주석 (Style)

**파일**: `src/builtins/BuiltinArray.cpp`  
**함수**: `builtinArrayLastIndexOf`

```diff
-    // If argument fromIndex was passed let n be ToInteger(fromIndex); else let n be len-1.
+    /*
+        If argument fromIndex was passed let n be ToInteger(fromIndex); else let n be len-1.
+    */
```

기능 변경 없이 `//` 주석을 `/* */` 블록으로 변환. 프로젝트 전반에 걸친 스타일 불일치를 유발한다.

## 함정 7 - 함수 자주 호출

```diff
-size_t len = str->length();
-for (size_t i = 0; i < len; i++) {
+for (size_t i = 0; i < str->length(); i++) {
```

## 함정 8 - 주석 단일행 주석 -> 블록 주석 (Style)

**파일**: `src/builtins/BuiltinArray.cpp`
```diff
-// If n ≥ 0, then let k be min(n, len – 1).
+/*
+If n ≥ 0, then let k be min(n, len – 1).
+*/
```
기능 변경 없이 `//` 주석을 `/* */` 블록으로 변환. 프로젝트 전반에 걸친 스타일 불일치를 유발한다.
