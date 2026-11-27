package dev.incidentlab;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import org.springframework.stereotype.Component;

@Component
public class FaultRegistry {
  private final Map<String, AtomicBoolean> enabled = new ConcurrentHashMap<>();

  public boolean isEnabled(String id) {
    return enabled.getOrDefault(id, new AtomicBoolean()).get();
  }

  public void enable(String id) {
    enabled.computeIfAbsent(id, ignored -> new AtomicBoolean()).set(true);
  }

  public void disable(String id) {
    enabled.computeIfAbsent(id, ignored -> new AtomicBoolean()).set(false);
  }

  public void reset() {
    enabled.values().forEach(flag -> flag.set(false));
  }

  public Map<String, AtomicBoolean> all() {
    return enabled;
  }
}