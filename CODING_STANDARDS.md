# Coding Standards

## Guiding Principle: Simple, Maintainable, Understandable

**Write code for humans first, computers second.**

Our goal is industrial-grade software that works reliably for years - like a car that runs 300,000 miles. This requires code that any team member can understand, maintain, and extend six months from now without the original context.

### Core Values

**Boring is Better**: Prefer obvious solutions over clever ones. If it takes more than 30 seconds to understand what code does, it's too clever.

**Explicit Over Implicit**: Make behavior visible. Hidden magic creates maintenance nightmares.

**Simplicity Over Cleverness**: The simplest solution that completely solves the problem wins. Every line of complexity is a liability.

**Readable Over Compact**: Code is read 10x more than it's written. Optimize for the reader.

---

## The Toyota Principle: Five Essential Pillars

Every piece of code must satisfy ALL five pillars. This is **non-negotiable** for industrial-grade reliability.

### 1. Not Over-Engineered

**Rule**: Implement only what's currently needed (YAGNI). Simplest solution that completely solves the problem.

**Good**: Direct solution that solves the actual problem
```python
# Clear and direct
def calculate_discount(price, discount_percent):
    return price * (1 - discount_percent / 100)
```

**Bad**: Abstract factory pattern for a simple calculation
```python
# Over-engineered - don't do this
class DiscountCalculatorFactory:
    def create_calculator(self, strategy_type):
        # 50 lines of unnecessary abstraction...
```

**The Test**: If you can't explain why the complexity is needed, remove it.

---

### 2. Sophisticated Where Needed

**Rule**: Add complexity ONLY for reliability, security, or performance. No unnecessary abstractions.

Use proven patterns. Follow SOLID principles:
- **Single Responsibility**: One reason to change
- **Open/Closed**: Extend without modification
- **Dependency Inversion**: Depend on abstractions
- **DRY**: Eliminate duplication (but don't abstract prematurely)

**When to Add Complexity**:
- Thread-safety for concurrent access
- Security validation for untrusted input
- Performance optimization (after measuring!)
- Graceful degradation for production systems

**When NOT to Add Complexity**:
- "We might need it someday"
- "This is more elegant"
- "I read about this pattern"

---

### 3. Robust Error Handling

**Rule**: Fail fast with specific errors and actionable messages. Structured logging with context.

**Error Message Pattern**:
```
throw new [SpecificErrorType](
    `[What failed] in [where]: [details]. ` +
    `Context: [relevant values]. ` +
    `Fix: [actionable guidance]. ` +
    `Docs: [link if available]`
);
```

**Good Error**:
```python
raise ValueError(
    f"Invalid temperature {temp}°C in sensor_id={sensor_id}. "
    f"Must be between -40°C and 85°C. "
    f"Check sensor calibration. Docs: https://..."
)
```

**Bad Error**:
```python
raise Exception("Invalid value")  # What value? Where? How to fix?
```

**Rules**:
- Catch specific exception types, never bare `except` or `catch (Exception)`
- Provide actionable messages (tell operators what to do)
- Fail-fast on invalid configuration at startup
- Structured logging with context (who, what, when, where, why)

---

### 4. Complete Observability

**Rule**: Log everything with context. You can't fix what you can't see.

Log structured events for:
- ✅ Success with key metrics (duration, record count)
- ✅ Failure with full context for debugging
- ✅ State changes with before/after values
- ❌ NOT debug spam or obvious operations

**Good**:
```python
logger.info("order_processed", extra={
    "order_id": order_id,
    "customer_id": customer_id,
    "total": total,
    "duration_ms": duration
})
```

**Bad**:
```python
logger.info("Processing...")
logger.info("Done")
```

---

### 5. Proven Patterns

**Rule**: Follow standard language/framework patterns. Don't write Java in Python or Python in C#.

- Use language idioms
- Follow framework conventions
- Standard interfaces for dependency injection
- No global state or singletons unless truly justified

---

## The Unix Philosophy: Seven Rules for Design Elegance

The Toyota principles focus on **operational reliability** (WHAT to achieve). Unix Philosophy complements this with **design elegance** (HOW to achieve it).

### Rule of Representation: "Fold Knowledge into Data"

**Principle**: Store knowledge in data structures (JSON, config) so program logic can be simple and robust.

**Why**: Adding new business rules requires zero code changes, only configuration updates.

```
// ANTI-PATTERN: Hard-coded knowledge
if (error.Contains("Access denied"))
    return new Classification { Type = "System" };
else if (error.Contains("Material allocation"))
    return new Classification { Type = "Manufacturing" };
// ... 178 more if/else statements

// UNIX PATTERN: Knowledge in data
var pattern = _patterns.FirstOrDefault(p => p.Regex.IsMatch(error));
return BuildClassification(pattern);  // Simple, stupid, robust
```

**Current implementation patterns**:
- Business rules: Configuration files (JSON, YAML, etc.)
- Error patterns: Data-driven matching
- Validation rules: Declarative where possible
- Workflow definitions: Data not code

---

### Rule of Least Surprise

**Principle**: Follow conventions. Methods do what names suggest.

- Consistent naming across the codebase
- If you call it `create` in one place, don't call it `add` elsewhere
- Throw on bad input, don't return null silently
- Use `Async` suffix for async methods (C#) or follow language convention

---

### Rule of Modularity

**Principle**: One responsibility per class/module, but keep cohesive logic together.

- Split at 300+ lines IF multiple concerns exist
- Don't split for arbitrary line count reasons
- Each function/class has one clear purpose:
  - ✅ `validateOrderData()`
  - ✅ `calculateShippingCost()`
  - ❌ `validateAndCalculateAndSave()`

---

### Rule of Separation

**Principle**: Separate policy (what) from mechanism (how).

- Business rules in configuration, algorithms in code
- Validation logic separate from business logic
- Logging concerns separate from core functionality
- Policy changes shouldn't require mechanism changes

---

### Rule of Composition

**Principle**: Standard interfaces. DI-friendly. No global state.

- Components work together through clean interfaces
- Constructor injection preferred
- No service locator anti-pattern
- Works cleanly with any application host

---

### Rule of Silence (adapted for ops)

**Principle**: Log structured events for audit/diagnostics. Avoid debug spam.

- No noise in production
- Every log entry has a purpose
- Structured fields, not string concatenation
- Log levels used correctly (Error vs Warn vs Info)

---

### Rule of Repair

**Principle**: Fail noisily with actionable guidance. (Linked to Toyota Pillar #3)

Tell user:
- **What** failed
- **Where** it failed
- **Why** it failed
- **How** to fix it
- **Where** to learn more

---

## Practical Guidelines

### Guard Clauses — Reduce Nesting

**Bad**:
```javascript
if (user) {
    if (user.isActive) {
        if (user.hasPermission) {
            // finally do the work
        }
    }
}
```

**Good**:
```javascript
if (!user) return;
if (!user.isActive) return;
if (!user.hasPermission) return;

// do the work
```

### Naming — Make Intent Clear

| Good | Bad |
|------|-----|
| `getUsersByRegistrationDate(startDate, endDate)` | `getUsers(d1, d2)` |
| `isEligibleForDiscount()` | `checkFlag()` |
| `customerRepository` | `custRepo` |

Be consistent: if you call it `create` in one place, don't call it `add` elsewhere.

### Input Validation

Validate all inputs at function entry:
```python
def process_order(order_id, quantity):
    if order_id is None or order_id <= 0:
        raise ValueError(f"Invalid order_id: {order_id}")
    if quantity <= 0 or quantity > 10000:
        raise ValueError(f"Invalid quantity: {quantity}. Must be 1-10000.")
```

---

## Defensive Coding (Critical)

### Universal Principles (All Languages)

- **Guard clauses** at function entry - validate all inputs before processing
- **Null/undefined safety** - Check for null/undefined, use optional chaining where available
- **Range validation** - Validate numeric ranges, string lengths, collection sizes
- **Type safety** - Use safe parsing/conversion patterns, validate types at runtime where needed
- **Regex timeouts** - **ALL regex patterns MUST have timeouts to prevent ReDoS attacks** (Critical!)
- **Path validation** - Validate and sanitize all file paths to prevent traversal attacks
- **Specific errors** - Use specific error types with actionable messages, never generic errors
- **Actionable messages** - Error messages must explain what failed, why, and how to fix

### Regex Timeout Examples by Language

**Python**:
```python
import regex  # Use 'regex' library (not 're') for timeout support
result = regex.match(pattern, text, timeout=1.0)  # 1 second timeout
```

**JavaScript/TypeScript**:
```typescript
// No native timeout - wrap in Promise with timeout or use worker threads
// Consider libraries like 're2' for safer regex matching
```

**C#**:
```csharp
var regex = new Regex(pattern, RegexOptions.Compiled, TimeSpan.FromMilliseconds(100));
```

**Java**:
```java
// Use context with timeout or implement custom timeout wrapper
```

**Go**:
```go
// Use context with timeout for regex operations
```

### Error Message Pattern

```
throw new [SpecificErrorType](
    `[What failed] in [where]: [details]. ` +
    `Context: [relevant values]. ` +
    `Fix: [actionable guidance]. ` +
    `Docs: [link to documentation]`
);
```

---

## Testing Standards

- **Pattern**: Arrange-Act-Assert (AAA) or Given-When-Then
- **Naming**: Descriptive test names that explain scenario and expected outcome
- **Independence**: Tests run in isolation, no shared state
- **Clarity**: Tests should read like documentation
- **Coverage**: Critical paths must have tests
- **One logical assertion** per test

---

## Code Review Checklist

Before merging, verify:

### Toyota Pillars
- [ ] **Not Over-Engineered**: Simplest solution that fully solves the problem? YAGNI applied?
- [ ] **Sophisticated Where Needed**: Thread-safe/concurrent-safe where needed?
- [ ] **Robust Error Handling**: Errors fail-fast with actionable messages?
- [ ] **Complete Observability**: Success AND failure logged with context?
- [ ] **Proven Patterns**: Uses standard patterns for this tech stack?

### Unix Philosophy
- [ ] **Representation**: Knowledge in data/config, not hardcoded?
- [ ] **Least Surprise**: API behavior matches expectations?
- [ ] **Modularity**: Single responsibility maintained?
- [ ] **Separation**: Policy (what) separate from mechanism (how)?
- [ ] **Composition**: Integrates cleanly with other systems?
- [ ] **Silence**: Logging useful without noise?
- [ ] **Repair**: Errors provide actionable guidance?

### Security & Defensive
- [ ] All inputs validated with guard clauses?
- [ ] **ALL regex patterns have timeouts?** (Critical!)
- [ ] File paths validated against traversal?
- [ ] Null/undefined checks in place?

### Simplicity Check
- [ ] Would I understand this in 6 months?
- [ ] Could a junior developer maintain this?
- [ ] Is this boring? (Good! Boring is maintainable)

### The Final Question
**Would this pass the Toyota Test for mission-critical code?**

---

## When to Violate Principles

Violate when necessary, but document why:

**Valid Reasons**: Performance bottleneck (proven by benchmarks), external constraints, regulatory requirements

**Document violations**:
```
// PRINCIPLE VIOLATION: [which principle]
// Reason: [why this is necessary]
// TODO(#issue): [plan to address when possible]
```

---

## Remember

**"Any fool can write code that a computer can understand. Good programmers write code that humans can understand."** - Martin Fowler

**"Debugging is twice as hard as writing code. So if you write code as cleverly as possible, you are, by definition, not smart enough to debug it."** - Brian Kernighan

Write today with tomorrow's maintainer in mind. That person might be you.
