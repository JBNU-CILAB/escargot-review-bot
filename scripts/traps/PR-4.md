# PR-4 함정 목록

- **base**: `eeea83ef3ef89d2254ade380d3a9e03dd729e3e3`
- **head**: `41c6719199b31502039d72e95e2230a52e5b1dec`
- **변경 파일**: BuiltinArray.cpp, BuiltinArrayBuffer.cpp, BuiltinError.cpp, BuiltinMath.cpp, BuiltinTypedArray.cpp, ISO8601.h

---

## 함정 1 — `size_t`로 인한 무한 루프 (Defect)

**파일**: `src/builtins/BuiltinArray.cpp`  
**함수**: `builtinArrayLastIndexOf`

```diff
-    int64_t k = doubleK;
+    size_t k = doubleK;
     // Repeat, while k≥ 0
     while (k >= 0) {
         ...
         k--;
     }
```

`size_t`는 unsigned 정수형이므로 `k >= 0` 조건은 항상 `true`.  
`k == 0`일 때 `k--`를 수행하면 언더플로우로 `SIZE_MAX`가 되어 **무한 루프** 발생.  
`Array.prototype.lastIndexOf(x)` 호출 시 엔진이 응답 불가 상태가 된다.

---

## 함정 2 — 버퍼 크기 `std::min` 제거로 인한 오버플로우 (Defect)

**파일**: `src/builtins/BuiltinArrayBuffer.cpp`  
**함수**: `builtinArrayBufferTransferToFixedLength`

```diff
-    newValue->fillData(obj->data(), std::min(newByteLength, static_cast<uint64_t>(obj->byteLength())));
+    newValue->fillData(obj->data(), static_cast<uint64_t>(obj->byteLength()));
```

`transferToFixedLength(n)` 호출 시 `n < 원본.byteLength`인 경우,  
새 버퍼(`newByteLength`만큼 할당)에 원본 전체(`byteLength`)를 복사하게 되어 **힙 버퍼 오버플로우** 발생.

---

## 함정 3 — 조기 반환 시 메모리 누수 (Defect)

**파일**: `src/builtins/BuiltinError.cpp`  
**함수**: `installErrorCause`

```diff
+    char* tempStr = new char[256];
+    if (options.isNumber()) {
+        return;          // ← delete[] 없이 반환
+    }
     ...
+    delete[] tempStr;
```

`options`가 숫자형이면 `return`으로 빠져나가며 `tempStr`이 해제되지 않아 **메모리 누수**. 해당 코드 자체도 완전히 불필요한 할당이다.

---

## 함정 4 — `Math.max()` 무인수 호출 시 OOB 접근 (Defect)

**파일**: `src/builtins/BuiltinMath.cpp`  
**함수**: `builtinMathMax`

```diff
-    if (argc == 0) {
-        return Value(Value::NegativeInfinityInit);
-    }
-
     double maxValue = argv[0].toNumber(state);   // argc == 0 이면 argv[0] 접근
```

ECMAScript 명세상 `Math.max()` (인수 없음)은 `-Infinity`를 반환해야 함.  
가드가 제거되어 `argc == 0`일 때 `argv[0]`에 접근 → **범위 밖 메모리 읽기 (UB)**.

---

## 함정 5 — `constexpr` 누락으로 인한 스택 오버헤드 (Compiler)

**파일**: `src/builtins/BuiltinTypedArray.cpp`  
**함수**: `builtinUint8ArrayToHex`

```diff
-    constexpr char radixDigits[] = "0123456789abcdefghijklmnopqrstuvwxyz";
+    const char radixDigits[] = "0123456789abcdefghijklmnopqrstuvwxyz";
```

`constexpr`이 있으면 컴파일러가 `.rodata`에 배치하지만, `const char[]`는 함수 진입 시마다 37바이트를 스택에 복사.  
`Uint8Array.prototype.toHex()` 호출 빈도가 높으면 누적 오버헤드 발생.

---

## 함정 6 — `final` 제거로 의도치 않은 상속 허용 (Refactor)

**파일**: `src/util/ISO8601.h`  
**클래스**: `InternalDuration`

```diff
-class InternalDuration final {
+class InternalDuration {
```

`final`이 제거되어 `InternalDuration`을 서브클래싱할 수 있게 됨.  
클래스에 가상 소멸자가 없으므로, 파생 클래스 포인터를 기반 클래스 포인터로 삭제 시 **UB** 발생 가능.

## 함정 7 - 'Coding Style Guide' 위배 중괄호 제거

**파일**: `src/builtins/BuiltinArray.cpp`
```diff
-if (len == 0) {
+if (len == 0)
        return Value(-1);
-}
```