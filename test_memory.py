"""Quick self-test for MemoryManager."""
import sys
sys.path.insert(0, "D:/AI/JARVIS")

from Core.Memory.memory_manager import MemoryManager

mm = MemoryManager()
mm.clear_history()

# Test facts
mm.set_fact("user", "name", "سلام، من علی هستم", 8)
mm.set_fact("user", "age", "۲۵ سال", 6)
mm.set_fact("preferences", "language", "fa", 9)

# Test conversation
mm.add_message("s1", "user", "سلام، چطوری؟", "fa")
mm.add_message("s1", "assistant", "سلام! خوبم، ممنون.", "fa")

# Test get_facts_prompt_block
print("=== Facts Prompt Block ===")
print(mm.get_facts_prompt_block())

# Test search
print("\n=== Search 'سلام' ===")
results = mm.search_conversations("سلام")
for r in results:
    print(f"  [{r['role']}] {r['content']}")

# Test preferences
mm.set_preference("timezone", "Asia/Tehran")
mm.set_preference("theme", "dark")
print("\n=== Preferences ===")
for p in mm.get_all_preferences():
    print(f"  {p['key']}: {p['value']}")

# Test stats
print("\n=== Stats ===")
print(mm.get_stats())

# Test delete
mm.delete_fact(1)
print(f"\nAfter delete_fact(1): {mm.get_stats()}")

# Test singleton
mm2 = MemoryManager.get_memory_manager()
print(f"\nSingleton works: {mm is mm2}")

print("\nDONE")
