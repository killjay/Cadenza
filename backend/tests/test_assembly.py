import pytest
from cadenza_contracts.ledger import (
    Ledger, Assembly, Part, Component, Joint, BoxFeature, BoxParameters,
    TargetRef, Placement
)
from cadenza_backend.geometry_bridge import build
from cadenza_backend.contracts_bridge import ErrorCode

def test_valid_assembly():
    box1 = Part(
        id="part_box1",
        features=[
            BoxFeature(
                id="feat_box1",
                operation="add",
                parameters=BoxParameters(length=10, width=10, height=10)
            )
        ]
    )
    box2 = Part(
        id="part_box2",
        features=[
            BoxFeature(
                id="feat_box2",
                operation="add",
                parameters=BoxParameters(length=10, width=10, height=10)
            )
        ]
    )
    
    comp1 = Component(id="comp_1", part_id="part_box1", placement=Placement(origin=[0, 0, 0]))
    # Place it adjacent to box1
    comp2 = Component(id="comp_2", part_id="part_box2", placement=Placement(origin=[10, 0, 0]))
    
    ledger = Ledger(
        features=[],
        assembly=Assembly(
            parts=[box1, box2],
            components=[comp1, comp2],
            joints=[]
        )
    )
    
    outcome = build(ledger)
    assert outcome.ok
    assert outcome.volume_mm3 == pytest.approx(2000.0)

def test_interference_error():
    box1 = Part(
        id="part_box1",
        features=[
            BoxFeature(
                id="feat_box1",
                operation="add",
                parameters=BoxParameters(length=10, width=10, height=10)
            )
        ]
    )
    box2 = Part(
        id="part_box2",
        features=[
            BoxFeature(
                id="feat_box2",
                operation="add",
                parameters=BoxParameters(length=10, width=10, height=10)
            )
        ]
    )
    
    comp1 = Component(id="comp_1", part_id="part_box1", placement=Placement(origin=[0, 0, 0]))
    # Place it overlapping with box1 (origin=[5, 0, 0] means they overlap by 5 units in X)
    comp2 = Component(id="comp_2", part_id="part_box2", placement=Placement(origin=[5, 0, 0]))
    
    ledger = Ledger(
        features=[],
        assembly=Assembly(
            parts=[box1, box2],
            components=[comp1, comp2],
            joints=[]
        )
    )
    
    outcome = build(ledger)
    assert not outcome.ok
    assert outcome.code == ErrorCode.GEOMETRY_KERNEL_FAILURE
    assert "Interference detected" in str(outcome.detail)

