# Coding Principles: Toyota + Unix Philosophy Deep Dive

This document provides the full intellectual context behind our coding standards. For the quick reference, see `CLAUDE.md`. For detailed standards, see `CODE_STANDARDS.md`.

---

## Why These Principles?

We build industrial-grade software for manufacturing environments where:
- Failures cost real money (production downtime, shipping errors, inventory mismatches)
- Systems run 24/7 in environments where restarts are costly
- Multiple developers maintain code over years, often without the original author
- Customers range from small shops to $1B+ operations

This demands code that is **simple enough to understand, robust enough to trust, and maintainable enough to evolve**.

---

## Part 1: The Toyota Production System Applied to Software

### Origin

The Toyota Production System (TPS) revolutionized manufacturing by focusing on eliminating waste, building quality in, and continuous improvement. These principles translate directly to software development.

### The Car Analogy

Think of your code like a Toyota that needs to run 300,000 miles:
- **Reliable**: Starts every time, handles bad roads, bad weather, bad fuel
- **Simple**: A mechanic can diagnose and fix problems quickly
- **Efficient**: No unnecessary parts, no wasted energy
- **Observable**: Dashboard tells you everything you need to know
- **Standard**: Parts are interchangeable, maintenance procedures are documented

### The Three Questions (Before Any Change)

1. **Does this make the code easier to understand?**
2. **Would I want to maintain this in six months?**
3. **Can a new team member grasp this quickly?**

If the answer to any of these is "no," reconsider.

### The Five Pillars — In Depth

#### Pillar 1: Not Over-Engineered

**The Core Idea**: Implement only what's currently needed. Every line of code is a liability — it must be maintained, tested, debugged, and understood. Unnecessary code is waste.

**The YAGNI Principle**: "You Aren't Gonna Need It." Don't build for hypothetical future requirements. Build for today's actual needs and refactor when real requirements emerge.

**Signs of Over-Engineering**:
- Abstract factory patterns for simple object creation
- Generic type parameters where concrete types would suffice
- Event-driven architectures for linear workflows
- Repository abstractions over already-abstract ORMs
- Configuration systems for values that never change
- Plugin architectures with only one plugin

**The Test**: Show your code to a colleague. If they say "why is this so complicated?", you've over-engineered it.

**The Tension**: This pillar exists in tension with Pillar 2 (Sophisticated Where Needed). The resolution: add complexity only when you can articulate the specific reliability, security, or performance requirement that demands it.

---

#### Pillar 2: Sophisticated Where Needed

**The Core Idea**: Some problems genuinely require sophisticated solutions. The key word is "needed" — complexity must earn its place.

**Justified Complexity**:
- **Thread-safety**: Concurrent access to shared state requires locks, semaphores, or immutable data structures
- **Security**: Input validation, constant-time comparison, rate limiting, path traversal prevention
- **Data Integrity**: Retry policies, idempotency, transactions, optimistic concurrency
- **Performance**: Connection pooling, batching, caching, async patterns (but only after measuring!)
- **Graceful Degradation**: Circuit breakers, fallback behaviors, health checks

**Unjustified Complexity**:
- "We might need it someday" — YAGNI
- "This is more elegant" — Elegant for whom?
- "I read about this pattern" — Patterns solve problems; don't create problems for patterns
- "Best practice says..." — Best practice for what context?

**The Test**: Can you articulate the specific failure mode this complexity prevents? If not, remove it.

---

#### Pillar 3: Robust Error Handling

**The Core Idea**: When things go wrong (and they will), the system should fail fast, fail clearly, and provide enough information for someone to fix the problem without debugging the source code.

**The Error Message Standard**:
Every error message should answer five questions:
1. **What** failed? (Specific operation or component)
2. **Where** did it fail? (File, method, line context)
3. **Why** did it fail? (The specific condition that triggered the error)
4. **How** to fix it? (Actionable guidance for the operator)
5. **Where** to learn more? (Documentation link if available)

**Anti-Patterns**:
- `throw new Exception("Error")` — What error? Where? How to fix?
- `catch (Exception ex) { /* ignore */ }` — Swallowing exceptions hides bugs
- `return null` — Null is not an error message
- `log.Error(ex.Message)` — Lost stack trace, lost context

**The Hierarchy**:
1. **Prevent** errors through validation (guard clauses, type safety)
2. **Detect** errors early (fail-fast on invalid state)
3. **Report** errors clearly (actionable messages with context)
4. **Recover** gracefully where possible (retry, fallback, circuit breaker)

---

#### Pillar 4: Complete Observability

**The Core Idea**: You can't fix what you can't see. Every significant operation should leave a structured trace that enables diagnosis without attaching a debugger.

**What to Log**:
- ✅ Successful operations with key metrics (duration, record count, affected entities)
- ✅ Failed operations with full context for reproduction
- ✅ State changes with before/after values
- ✅ External system interactions (API calls, database queries, file operations)
- ✅ Security-relevant events (authentication, authorization, data access)

**What NOT to Log**:
- ❌ Every loop iteration or trivial operation
- ❌ Sensitive data (passwords, tokens, PII) without masking
- ❌ Debug-level detail in production
- ❌ Redundant information already captured elsewhere

**Structured Logging**:
Always use structured logging (key-value pairs), never string concatenation:
```
// Bad: Hard to parse, hard to search
logger.info($"Processed order {orderId} for customer {customerId} in {duration}ms")

// Good: Machine-parseable, searchable, filterable
logger.info("order_processed", {
    order_id: orderId,
    customer_id: customerId,
    duration_ms: duration,
    item_count: items.length
})
```

---

#### Pillar 5: Proven Patterns

**The Core Idea**: Use battle-tested patterns that your team already knows. Don't invent new paradigms when existing ones work.

**Language Idioms**: Write idiomatic code for your language. Don't write Java in Python or Python in C#.

**Framework Conventions**: Follow the framework's intended patterns. If using ASP.NET, use dependency injection. If using Django, use models and views. Don't fight the framework.

**Standard Patterns**:
- Repository for data access abstraction
- Factory for complex object creation
- Strategy for interchangeable algorithms
- Observer/Event for decoupled notifications
- Options/Configuration for settings

**The Test**: Could a new developer who knows the language and framework understand this code without learning a custom architecture?

---

## Part 2: The Unix Philosophy Applied to Software Design

### Origin

The Unix Philosophy emerged from decades of building systems that compose, scale, and endure. These principles have survived 50+ years because they work.

### The Seven Rules — In Depth

#### Rule of Representation: "Fold Knowledge into Data"

**The Full Principle**: Store knowledge in data structures so program logic can be simple and robust. Data is more tractable than program logic. It follows that where you see a choice between complexity in data structures and complexity in code, choose the former.

**Why This Matters for Industrial Software**: Manufacturing rules change constantly — new products, new processes, new regulations. If rules are in code, every change requires a developer, a build, a deployment, and a test cycle. If rules are in data, an operator can update them.

**Implementation Patterns**:
- Business rules → JSON/YAML configuration files
- Error patterns → Data-driven pattern matching
- Validation rules → Declarative schemas
- Workflow definitions → State machine configurations
- Feature flags → Configuration, not if/else branches

---

#### Rule of Least Surprise

**The Full Principle**: In interface design, always do the least surprising thing. Make the behavior of a program match what the user (or calling code) would expect.

**Practical Application**:
- Method names describe exactly what they do
- `getUser()` returns a user, doesn't modify state
- `deleteOrder()` deletes, doesn't archive
- Consistent naming: if you call it `create` in one module, don't call it `add` in another
- Return types match expectations: don't return null when the caller expects an object
- Side effects are explicit, never hidden

---

#### Rule of Modularity

**The Full Principle**: Write simple parts connected by clean interfaces. Each module should do one thing well.

**The Balance**: Don't split for arbitrary line counts. Split when a module has multiple responsibilities. A 400-line class with one clear purpose is better than four 100-line classes with tangled dependencies.

**Signs You Need to Split**:
- The class name contains "And" or "Manager"
- Changes to one feature require changes to unrelated code in the same file
- You can't describe the class's purpose in one sentence
- Different parts of the class change at different rates

---

#### Rule of Separation

**The Full Principle**: Separate policy from mechanism. Separate interfaces from engines.

**In Practice**:
- Business rules (policy) live in configuration; algorithms (mechanism) live in code
- Validation rules (what's valid) are separate from validation execution (how to check)
- Logging decisions (what to log) are separate from logging infrastructure (how to log)
- API contracts (what's exposed) are separate from implementation (how it works)

---

#### Rule of Composition

**The Full Principle**: Design programs to be connected to other programs. Use standard interfaces.

**In Practice**:
- Constructor injection for dependencies
- Standard interfaces (not custom base classes) for extensibility
- No global state or static mutable variables
- Async/await compatible for modern applications
- Works with any DI container, any host, any test framework

---

#### Rule of Silence

**The Full Principle**: When a program has nothing surprising to say, it should say nothing. Adapted for modern observability: log structured events for audit/diagnostics, but avoid noise.

**The Balance**: Unix originally meant "produce no output on success." For industrial software, we need observability. The adaptation: log meaningfully, not verbosely.

**Production Log Levels**:
- **Error**: Something failed that needs human attention
- **Warning**: Something unexpected that might need attention
- **Info**: Significant state changes, operations completed
- **Debug**: Detailed diagnostic info (disabled in production)

---

#### Rule of Repair

**The Full Principle**: When you must fail, fail noisily and as soon as possible. Repair what you can — but when you must fail, fail noisily.

**This Reinforces Toyota Pillar #3** (Robust Error Handling). When combined:
- Validate early, fail fast
- Error messages are actionable
- Recovery is attempted where safe
- Unrecoverable failures are loud and clear

---

## Part 3: When Principles Conflict

### Over-Engineered vs. Sophisticated

**Resolution**: Can you articulate the specific failure mode? If yes → sophisticated. If no → over-engineered.

### Simplicity vs. Observability

**Resolution**: Logging infrastructure should be simple. What you log should be comprehensive. Use structured logging libraries, not custom frameworks.

### Modularity vs. Simplicity

**Resolution**: Don't create abstractions for their own sake. A single well-organized file is better than five files with complex interdependencies.

### Speed vs. Quality

**Resolution**: Never sacrifice quality for speed. Technical debt compounds faster than financial debt. The "quick hack" that ships today creates the outage that costs 10x next month.

---

## Summary

These principles aren't rules to follow blindly. They're mental models that help you make better decisions:

1. **Toyota** asks: Is this reliable enough for mission-critical use?
2. **Unix** asks: Is this designed for composability and long-term maintenance?
3. **Together** they produce: Simple, robust, observable, maintainable software.

When in doubt, ask: **"Would I trust this code to run my factory floor?"**
