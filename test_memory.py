'''Quick self-test for MemoryManager.'''
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from Core.Memory.memory_manager import MemoryManager

mm = MemoryManager()
mm.clear_history()

# Test facts
mm.set_fact('user', 'name', 'سلام، من علی هستم', 8)
mm.set_fact('user', 'age', '۲۵ سال', 6)
mm.set_fact('preferences', 'language', 'fa', 9)

# Test conversation
mm.add_message('s1', 'user', 'سلام، چطوری؟', 'fa')
mm.add_message('s1', 'assistant', 'سلام! خوبم، ممنون.', 'fa')

# Test get_facts_prompt_block
print('=== Facts Prompt Block ===')
print(mm.get_facts_prompt_block())

# Test get_all_facts & list_facts
print('\n=== All Facts ===')
all_facts = mm.get_all_facts()
for f in all_facts:
    print(f"  [{f['category']}] {f['key']} = {f['value']} (importance={f['importance']})")
assert len(all_facts) >= 3, 'Expected at least 3 facts'
assert 'id' in all_facts[0] and 'key' in all_facts[0] and 'fact_key' in all_facts[0] and 'value' in all_facts[0] and 'fact_value' in all_facts[0]

# Test search
print('\n=== Search "سلام" ===')
results = mm.search_conversations('سلام')
for r in results:
    print(f"  [{r['role']}] {r['content']}")
assert len(results) >= 2, 'Expected at least 2 messages found'

# Test preferences
mm.set_preference('timezone', 'Asia/Tehran')
mm.set_preference('theme', 'dark')
print('\n=== Preferences ===')
prefs = mm.get_all_preferences()
for k, v in prefs.items():
    print(f'  {k}: {v}')
assert 'theme' in prefs and prefs['theme'] == 'dark'

# Test stats
print('\n=== Stats ===')
stats = mm.get_stats()
print(stats)
assert stats['conversations'] >= 2
assert stats['facts'] >= 3
assert stats['preferences'] >= 2

# Test delete by string key and by ID
mm.set_fact('test_cat', 'test_delete_key', 'val', 5)
all_facts_updated = mm.get_all_facts()
target_fact = [f for f in all_facts_updated if f['key'] == 'test_delete_key'][0]

deleted_id = mm.delete_fact(target_fact['id'])
print(f"\nAfter delete_fact({target_fact['id']}): success={deleted_id}, stats={mm.get_stats()}")
assert deleted_id is True

mm.set_fact('test_cat', 'test_delete_key2', 'val2', 5)
deleted_key = mm.delete_fact('test_delete_key2')
print(f"\nAfter delete_fact('test_delete_key2'): success={deleted_key}, stats={mm.get_stats()}")
assert deleted_key is True

# Test singleton
mm2 = MemoryManager.get_memory_manager()
print(f'\nSingleton works: {mm is mm2}')
assert mm is mm2, 'Singleton instance mismatch!'

print('\nDONE')
