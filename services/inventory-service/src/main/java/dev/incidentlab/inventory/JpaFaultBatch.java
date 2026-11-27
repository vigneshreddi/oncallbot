package dev.incidentlab.inventory;

import jakarta.persistence.*;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;

@Entity
@Table(name = "jpa_fault_batch", schema = "inventory")
public class JpaFaultBatch {
  @Id
  private Long id;

  @Version
  private Long version;

  @Column(name = "reserved_qty", nullable = false)
  private int reservedQty;

  @Column(name = "updated_at", nullable = false)
  private Instant updatedAt;

  @OneToMany(mappedBy = "batch", fetch = FetchType.LAZY, cascade = CascadeType.ALL)
  private List<JpaFaultReservation> reservations = new ArrayList<>();

  protected JpaFaultBatch() {}

  JpaFaultBatch(Long id) {
    this.id = id;
    this.updatedAt = Instant.now();
    reservations.add(new JpaFaultReservation(id, this));
  }

  List<JpaFaultReservation> getReservations() {
    return reservations;
  }

  void reserveOne() {
    reservedQty++;
    updatedAt = Instant.now();
  }
}