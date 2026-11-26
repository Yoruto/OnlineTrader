"""Market Scenario Engine (Phase 1): general replay, channel faults, broker matching, and strict verdicts."""

from __future__ import annotations

from qa.scenario_engine.channel import MarketChannel
from qa.scenario_engine.corpus import MarketCorpus, compare_version_stress_reports
from qa.scenario_engine.matcher import OrderMatcher
from qa.scenario_engine.models import (
    BrokerMatchMode,
    ChannelFaultKind,
    ENGINE_VERSION,
    FaultRule,
    InstrumentSpec,
    MarketTickTruth,
    ObservedTick,
    ScenarioConfig,
    ScenarioResult,
    SessionWindow,
    TriggerKind,
    TriggerRecord,
    Verdict,
)
from qa.scenario_engine.monitor import (
    DegradationThresholds,
    ExternalPerformanceMonitor,
    compare_with_baseline,
)
from qa.scenario_engine.multiday import MultiDayConfig, MultiDayEnduranceRunner
from qa.scenario_engine.random_gen import ConstrainedRandomGenerator, FaultSeverity
from qa.scenario_engine.replay_source import RecordedReplaySource
from qa.scenario_engine.runner import MarketScenarioRunner
from qa.scenario_engine.sdk_bridge import (
    BridgeClock,
    DsvSdkAdapterBridge,
    FakeTqApi,
    build_calendar_from_sessions,
)
from qa.scenario_engine.segment_index import SegmentFeature, SegmentIndex, SegmentRegime
from qa.scenario_engine.shrinker import FailureShrinker
from qa.scenario_engine.stress_runner import RealtimeStressRunner, StressProfile
from qa.scenario_engine.synthesizer import (
    GenerationMode,
    MarketSynthesizer,
    SegmentSlice,
    SynthesisSpec,
)
from qa.scenario_engine.ui_stress import WorkbenchUIStressDriver

__all__ = [
    "BridgeClock",
    "BrokerMatchMode",
    "ChannelFaultKind",
    "ConstrainedRandomGenerator",
    "DegradationThresholds",
    "DsvSdkAdapterBridge",
    "ENGINE_VERSION",
    "ExternalPerformanceMonitor",
    "FailureShrinker",
    "FakeTqApi",
    "FaultRule",
    "FaultSeverity",
    "GenerationMode",
    "InstrumentSpec",
    "MarketChannel",
    "MarketCorpus",
    "MarketScenarioRunner",
    "MarketSynthesizer",
    "MarketTickTruth",
    "MultiDayConfig",
    "MultiDayEnduranceRunner",
    "ObservedTick",
    "OrderMatcher",
    "RealtimeStressRunner",
    "RecordedReplaySource",
    "ScenarioConfig",
    "ScenarioResult",
    "SegmentFeature",
    "SegmentIndex",
    "SegmentRegime",
    "SegmentSlice",
    "SessionWindow",
    "StressProfile",
    "SynthesisSpec",
    "TriggerKind",
    "TriggerRecord",
    "Verdict",
    "WorkbenchUIStressDriver",
    "build_calendar_from_sessions",
    "compare_version_stress_reports",
    "compare_with_baseline",
]
