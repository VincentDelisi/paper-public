"""Sample trading agents that run on their own Shadow Book paper accounts."""
from .base import Agent, AgentContext
from .demos import (CoveredCallWriter, DipBuyer, MomentumCallScalper, PutCreditSpreadSeller,
                    default_agents)
from .runner import AgentRunner

__all__ = ["Agent", "AgentContext", "AgentRunner", "DipBuyer", "MomentumCallScalper",
           "PutCreditSpreadSeller", "CoveredCallWriter", "default_agents"]
