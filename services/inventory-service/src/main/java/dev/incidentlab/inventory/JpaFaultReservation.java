package dev.incidentlab.inventory;

import jakarta.persistence.*;

@Entity
@Table(name = "jpa_fault_reservation", schema = "inventory")
public class JpaFaultReservation {
  @Id
  private Long id;

  @ManyToOne(fetch = FetchType.LAZY, optional = false)
  @JoinColumn(name = "batch_id", nullable = false)
  private JpaFaultBatch batch;

  protected JpaFaultReservation() {}

  JpaFaultReservation(Long id, JpaFaultBatch batch) {
    this.id = id;
    this.batch = batch;
  }
}