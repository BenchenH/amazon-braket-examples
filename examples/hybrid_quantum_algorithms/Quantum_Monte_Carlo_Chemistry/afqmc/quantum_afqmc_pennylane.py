#!/usr/bin/env

import os
import sys
import math
import cmath
import numpy as np
from tqdm import tqdm
import pennylane as qml
import multiprocessing as mp
from itertools import product
from scipy.linalg import expm, qr, det

from braket.circuits import Circuit, FreeParameter
from braket.devices import LocalSimulator
from braket.jobs.metrics import log_metric

from openfermion.circuits.primitives import bogoliubov_transform
from openfermion.circuits.primitives.state_preparation import prepare_slater_determinant
from openfermion.circuits.slater_determinants import slater_determinant_preparation_circuit
from openfermion.linalg.givens_rotations import givens_decomposition, givens_decomposition_square
from afqmc.classical_afqmc import G_pq, local_energy, PropagateWalker, ImagTimePropagator


def reortho(A):
    """Reorthogonalise a MxN matrix A.
    Performs a QR decomposition of A. Note that for consistency elsewhere we
    want to preserve detR > 0 which is not guaranteed. We thus factor the signs
    of the diagonal of R into Q.
    Parameters
    ----------
    A : :class:`np.ndarray`
        MxN matrix.
    Returns
    -------
    Q : :class:`np.ndarray`
        Orthogonal matrix. A = QR.
    detR : float
        Determinant of upper triangular matrix (R) from QR decomposition.
    """
    (Q, R) = qr(A, mode='economic')
    signs = np.diag(np.sign(np.diag(R)))
    Q = Q.dot(signs)
    detR = det(signs.dot(R))
    return (Q, detR)



# Implement Givens rotation
def givens_block_circuit(givens):
    '''This function defines the Givens rotation circuit from a single givens tuple
    Args:
        givens: (i, j, \theta, \varphi)
    '''
    (i, j, theta, varphi) = givens
    
    qml.RZ(-varphi, wires=j)
    qml.CNOT(wires=[j, i])
    
    # implement the cry rotation
    qml.RY(theta, wires=j)
    qml.CNOT(wires=[i, j])
    qml.RY(-theta, wires=j)
    qml.CNOT(wires=[i, j])
    
    qml.CNOT(wires=[j, i])



def prepare_slater_circuit(circuit_description):
    '''Creating Givens rotation circuit to prepare arbitrary Slater determinant.
    Args:
        circuit_description: list of tuples containing Givens rotation (i, j, theta, phi) in reversed order.
    '''
    
    for parallel_ops in circuit_description:
        for givens in parallel_ops:
            qml.adjoint(givens_block_circuit)(givens)



def circuit_first_half(Q):
    '''Construct the first half of the vacuum reference circuit
    Args:
        Q: orthonormalized walker state
    '''
    num_qubits, num_particles = Q.shape
    qml.Hadamard(wires=0)
    
    if num_particles > 1.0:
        for i in range(1, num_particles):
            qml.CNOT(wires=[0, i])
    
    complement = np.ones((num_qubits, num_qubits-num_particles))
    W, _ = reortho(np.hstack((Q, complement)))
    decomposition, diagonal = givens_decomposition_square(W.T)
    circuit_description = list(reversed(decomposition))
    
    for i in range(len(diagonal)):
        qml.RZ(np.angle(diagonal[i]), wires=i)
        
    prepare_slater_circuit(circuit_description)
    
    
def circuit_second_half_real(Q, V_T):
    '''Construct the second half of the vacuum reference circuit (for real expectation values)
    Args:
        Q: orthonormalized walker state
        V_T: quantum trial state
    '''
    num_qubits, num_particles = Q.shape
    qml.adjoint(V_T)()
    
    if num_particles > 1.0:
        for i in range(1, num_particles)[::-1]:
            qml.CNOT(wires=[0, i])
    qml.Hadamard(wires=0)
    
    
def circuit_second_half_imag(Q, V_T):
    '''Construct the second half of the vacuum reference circuit (for imaginary expectation values)
    Args:
        Q: orthonormalized walker state
        V_T: quantum trial state
    '''
    num_qubits, num_particles = Q.shape
    qml.adjoint(V_T)()
    
    if num_particles > 1.0:
        for i in range(1, num_particles)[::-1]:
            qml.CNOT(wires=[0, i])
    
    qml.S(wires=0)
    qml.S(wires=0)
    qml.S(wires=0)
    qml.Hadamard(wires=0)
    


def amplitude_real(Q, V_T):
    '''Construct the the vacuum reference circuit for measuring amplitude real part
    Args:
        Q: orthonormalized walker state
        V_T: quantum trial state
    '''
    circuit_first_half(Q)
    circuit_second_half_real(Q, V_T)
    


def amplitude_imag(Q, V_T):
    '''Construct the the vacuum reference circuit for measuring amplitude imaginary part
    Args:
        Q: orthonormalized walker state
        V_T: quantum trial state
    '''
    circuit_first_half(Q)
    circuit_second_half_imag(Q, V_T)
    
      

def amplitude_estimate(Q, V_T, dev):
    '''This function computes the amplitude between walker state and quantum trial state.
    Args:
        Q: orthonormalized walker state
        V_T: quantum trial state
        dev: qml.device('lightning.qubit', wires=wires) for simulator;
             qml.device('braket.aws.qubit', device_arn=device_arn, wires=wires, shots=shots) for real device;
    Returns:
        amplitude: numpy.complex128
    '''
    num_qubits, num_particles = Q.shape
        
    @qml.qnode(dev, interface=None, diff_method=None)
    def compute_real(Q, V_T):
        amplitude_real(Q, V_T)
        return qml.probs(range(num_qubits))

    probs_values = compute_real(Q, V_T)
    real = probs_values[0] - probs_values[int(2**num_qubits/2)]

    @qml.qnode(dev, interface=None, diff_method=None)
    def compute_imag(Q, V_T):
        amplitude_imag(Q, V_T)
        return qml.probs(range(num_qubits))

    probs_values = compute_imag(Q, V_T)
    imag = probs_values[0] - probs_values[int(2**num_qubits/2)]

    return (real + 1.j*imag)



def U_circuit(U):
    '''Construct circuit to perform unitary transformation U.'''
    
    decomposition, diagonal = givens_decomposition_square(U)
    circuit_description = list(reversed(decomposition))
    
    for i in range(len(diagonal)):
        qml.RZ(np.angle(diagonal[i]), i)
    
    if circuit_description != []:
        prepare_slater_circuit(circuit_description)
    
    

def pauli_real(Q, V_T, U, pauli):
    '''Construct the the vacuum reference circuit for measuring expectation value of a pauli real part
    Args:
        Q: orthonormalized walker state
        V_T: quantum trial state
        U: unitary transformation to change the Pauli into Z basis
        pauli: list that stores the position of the Z gate, e.g., [0,1] represents 'ZZII'.
    '''
    circuit_first_half(Q)
    
    U_circuit(U)
    for i in pauli:
        qml.PauliZ(wires=i)
        
    qml.adjoint(U_circuit)(U)
    circuit_second_half_real(Q, V_T)



def pauli_imag(Q, V_T, U, pauli):
    '''Construct the the vacuum reference circuit for measuring expectation value of a pauli imaginary part
    Args:
        Q: orthonormalized walker state
        V_T: quantum trial state
        U: unitary transformation to change the Pauli into Z basis
        pauli: list that stores the position of the Z gate, e.g., [0,1] represents 'ZZII'.
    '''
    circuit_first_half(Q)
    
    U_circuit(U)
    for i in pauli:
        qml.PauliZ(wires=i)
        
    qml.adjoint(U_circuit)(U)
    circuit_second_half_imag(Q, V_T)



def pauli_estimate(Q, V_T, U, pauli: list, dev):
    '''This function returns the expectation value of $\langle \Psi_Q|pauli|\phi_l\rangle$.
    Args:
        Q: np.ndarray; matrix representation of the walker state, not necessarily orthonormalized.
        V_T: circuit unitary to prepare the quantum trial state
        dev: qml.device('braket.aws.qubit', device_arn=device_arn, wires=wires, shots=shots),
             if shots is specified as nonzero
        U: eigenvector of Cholesky vectors, $L = U \lambda U^{\dagger}$
        pauli: Pauli string, e.g., [0,1] represents 'ZZII'.
        dev: qml.device('lightning.qubit', wires=wires) for simulator;
             qml.device('braket.aws.qubit', device_arn=device_arn, wires=wires, shots=shots) for real device;
    
    Returns:
        expectation value
    '''
    num_qubits, num_particles = Q.shape
    
    @qml.qnode(dev, interface=None, diff_method=None)
    def compute_real(Q, V_T, U, pauli):
        pauli_real(Q, V_T, U, pauli)
        return qml.probs(range(num_qubits))
    
    probs_values = compute_real(Q, V_T, U, pauli)
    real = probs_values[0] - probs_values[int(2**num_qubits/2)]
    
    @qml.qnode(dev, interface=None, diff_method=None)
    def compute_imag(Q, V_T, U, pauli):
        pauli_imag(Q, V_T, U, pauli)
        return qml.probs(range(num_qubits))
    
    probs_values = compute_imag(Q, V_T, U, pauli)
    imag = probs_values[0] - probs_values[int(2**num_qubits/2)]
    
    return (real + 1.j*imag)




def qExpect_OneBody(walker, one_bodies, ovlp, V_T, dev):
    '''This function computes the expectation value of one-body operator between quantum trial state and walker
    Args:
        walker: walker Slater determinant
        one_bodies: list of one_body operators whose expectation value is to be computed;
        ovlp: amplitude between walker and the quantum trial state
        V_T: quantum trial state
        dev: qml.device('lightning.qubit', wires=wires) for simulator;
             qml.device('braket.aws.qubit', device_arn=device_arn, wires=wires, shots=shots) for real device;
    
    Returns:
        value:
    '''
    num_qubits, num_particles = walker.shape
    I = np.identity(num_qubits)
    
    expectation = np.array([])
    Dict = {}
    for i in range(num_qubits):
        Dict.update({(i): pauli_estimate(walker, V_T, I, [i], dev)})
    
    for one_body in one_bodies:
        value = 0.0 + 0.0j
        # check if the one-body term is already diagonal or not
        if np.count_nonzero(np.round(one_body - np.diag(np.diagonal(one_body)), 7)) != 0:
            Dic = {}
            lamb, U = np.linalg.eigh(one_body)
            for i in range(num_qubits):
                Dic.update({(i): pauli_estimate(walker, V_T, U, [i], dev)})
                
            for i in range(num_qubits):
                expectation_value = 0.5*(ovlp - Dic.get((i)))
                value += lamb[i]*expectation_value
                
            expectation = np.append(expectation, value)
        else:
            for i in range(num_qubits):
                expectation_value = 0.5*(ovlp - Dict.get((i)))
                value += one_body[i,i]*expectation_value
            expectation = np.append(expectation, value)
    
    return expectation




def local_energy_quantum(walker, ovlp, one_body, lambda_l, U_l, V_T, dev):
    '''This function estimates the integral $\langle \Psi_Q|H|\phi_l\rangle$ with rotated basis.
    
    Args:
        walker: np.ndarray; matrix representation of the walker state, not necessarily orthonormalized.
        ovlp:
        one_body: (corrected) one-body term in the second quantized hamiltonian written in chemist's notation.
                  This term is assumed to be diagonal in the current implementation, but should be rather 
                  straight forward to generalize if it's not.
                  
        lambda_l: eigenvalues of Cholesky vectors
        U_l: eigenvectors of Cholesky vectors
        V_T: quantum trial state
        dev: qml.device('lightning.qubit', wires=wires) for simulator;
             qml.device('braket.aws.qubit', device_arn=device_arn, wires=wires, shots=shots) for real device;
    
    Returns:
        energy: complex
    '''
    energy = 0. + 0.j
    num_qubits, num_particles = walker.shape
    
    # one-body term assuming diagonal form already
    I = np.identity(num_qubits)
    Dic = {}
    for i in range(num_qubits):
        Dic.update({(i): pauli_estimate(walker, V_T, I, [i], dev)})
        for j in range(i+1, num_qubits):
            Dic.update({(i,j): pauli_estimate(walker, V_T, I, [i,j], dev)})
        
    for i in range(num_qubits):
        expectation_value = 0.5*(ovlp - Dic.get((i)))
        energy += one_body[i,i]*expectation_value
            
    # Cholesky decomposed two-body term
    for lamb, U in zip(lambda_l, U_l):
        # define a dictionary to store all the expectation values
        if np.count_nonzero(np.round(U - np.diag(np.diagonal(U)), 7)) == 0:
            Dict = Dic
            for i in range(num_qubits):
                for j in range(i, num_qubits):
                    if i == j:
                        expectation_value = 0.5*(ovlp - Dict.get((i)))
                    else:
                        expectation_value = 0.5*(ovlp - Dict.get((i)) - Dict.get((j)) + Dict.get((i,j)))
                    energy += 0.5*lamb[i]*lamb[j]*expectation_value
        else:
            Dict = {}
            for i in range(num_qubits):
                Dict.update({(i): pauli_estimate(walker, V_T, U, [i], dev)})
                for j in range(i, num_qubits):
                    Dict.update({(i,j): pauli_estimate(walker, V_T, U, [i,j], dev)})

            for i in range(num_qubits):
                for j in range(i, num_qubits):
                    if i == j:
                        expectation_value = 0.5*(ovlp - Dict.get((i)))
                    else:
                        expectation_value = 0.5*(ovlp - Dict.get((i)) - Dict.get((j)) + Dict.get((i,j)))
                    energy += 0.5*lamb[i]*lamb[j]*expectation_value
        
    return energy




def qPropagateWalker(x, v_0, v_gamma, mf_shift, dtau, walker, V_T, ovlp, dev):
    '''This function updates the walker from imaginary time propagation.
    
    Args:
        x: auxiliary fields
        v_0: modified one-body term from reordering the two-body operator + mean-field subtraction.
        v_gamma: Cholesky vectors stored in list (L, num_spin_orbitals, num_spin_orbitals), without mf_shift
        mf_shift: mean-field shift \Bar{v}_{\gamma} stored in np.array format
        dtau: imaginary time step size
        walker: walker state as np.ndarray, others are the same as trial
        V_T: quantum trial state
        ovlp: amplitude between walker and the quantum trial state
        dev: qml.device('lightning.qubit', wires=wires) for simulator;
             qml.device('braket.aws.qubit', device_arn=device_arn, wires=wires, shots=shots) for real device;
    
    Returns:
        new_walker:
        
    '''
    num_spin_orbitals, num_electrons = walker.shape
    num_fields = len(v_gamma)
    
    v_expectation = qExpect_OneBody(walker, v_gamma, ovlp, V_T, dev)
        
    xbar = -np.sqrt(dtau)*(v_expectation - mf_shift)
    # Sampling the auxiliary fields
    xshifted = x - xbar
    
    # Define the B operator B(x - \bar{x})
    exp_v0 = expm(-dtau/2*v_0)
    
    V = np.zeros((num_spin_orbitals, num_spin_orbitals), dtype=np.complex128)
    for i in range(num_fields):
        V += np.sqrt(dtau)*xshifted[i]*v_gamma[i]
    exp_V = expm(V)
    
    # Note that v_gamma doesn't include the mf_shift, there is an additional term coming from 
    # -(x - xbar)*mf_shift, this term is also a complex value.
    # cmf = -np.sqrt(dtau)*np.dot(xshifted, mf_shift)
    # prefactor = np.exp(-dtau*(H_0 - E_0) + cmf)
    B = exp_v0 @ exp_V @ exp_v0
    
    # Find the new walker state
    new_walker, _ = reortho(B @ walker)
    
    return new_walker




def ImagTimePropagator_QAEE(v_0, v_gamma, mf_shift, dtau, trial, walker, weight, h1e, eri, enuc, E_shift,
                            h_chem, lambda_l, U_l, V_T, dev):
    '''This function defines the imaginary propagation process and will return new walker state and new weight.
    
    Args:
        v_0: modified one-body term from reordering the two-body operator + mean-field subtraction.
        v_gamma: Cholesky vectors stored in list (L, num_spin_orbitals, num_spin_orbitals), without mf_shift
        mf_shift: mean-field shift \Bar{v}_{\gamma} stored in np.array format
        dtau: imaginary time step size
        trial: trial state as np.ndarray, e.g., for h2 HartreeFock state, it is np.array([[1,0], [0,1], [0,0], [0,0]])
        walker: normalized walker state as np.ndarray, others are the same as trial
        weight:
        h1e, eri: one-electron and two-electron integral stored in spatial orbitals
        enuc: nuclear repulsion energy
        E_shift: reference energy, usually taken as the HF energy.
        h_chem: modified one-body term from reordering the two-body operator
        lambda_l: eigenvalues of Cholesky vectors
        U_l: eigenvectors of Cholesky vectors
        V_T: quantum trial state
        dev: qml.device('lightning.qubit', wires=wires) for simulator;
             qml.device('braket.aws.qubit', device_arn=device_arn, wires=wires, shots=shots) for real device;
    
    Returns:
        E_loc:
        E_loc_q / c_ovlp: numerator 
        q_ovlp / c_ovlp: demoninator for evaluation of total energy
        new_weight:
        new_walker:
        
    '''
    seed = np.random.seed(int.from_bytes(os.urandom(4), byteorder='little'))
    
    # First compute the bias force using the expectation value of L operators
    num_spin_orbitals, num_electrons = trial.shape
    num_fields = len(v_gamma)
    I = np.identity(num_spin_orbitals)
    # compute the overlap integral
    ovlp = np.linalg.det(trial.transpose().conj() @ walker)
    
    trial_up = trial[::2,::2]
    trial_down = trial[1::2,1::2]
    walker_up = walker[::2,::2]
    walker_down = walker[1::2,1::2]
    G = [G_pq(trial_up, walker_up), G_pq(trial_down, walker_down)]
    E_loc = local_energy(h1e, eri, G, enuc)
    
    # Quantum-assisted energy evaluation
    # compute the overlap between qtrial state and walker
    c_ovlp = np.linalg.det(trial.transpose().conj() @ walker)
    q_ovlp = amplitude_estimate(walker, V_T, dev)        
    E_loc_q = (local_energy_quantum(walker, q_ovlp, h_chem, lambda_l, U_l, V_T, dev) + q_ovlp*enuc)
    
    # update the walker
    x = np.random.normal(0., 1., size=num_fields)
    new_walker = PropagateWalker(x, v_0, v_gamma, mf_shift, dtau, trial, walker, G)
    
    # Define the I operator and find new weight
    new_ovlp = np.linalg.det(trial.transpose().conj() @ new_walker)
    arg = np.angle(new_ovlp / ovlp)
    new_weight = weight*np.exp(-dtau*(np.real(E_loc) - E_shift))*np.max([0., np.cos(arg)])
    
    return E_loc, (E_loc_q/c_ovlp), (q_ovlp/c_ovlp), new_walker, new_weight



##############################################################
# Define V_T through UCCSD circuit.                          #
##############################################################
def V_T():
    qml.RX(np.pi/2., wires=0)
    for i in range(1, 4):
        qml.Hadamard(wires=i)

    for i in range(3):
        qml.CNOT(wires=[i, i+1])

    qml.RZ(0.12, wires=3)
    for i in range(3)[::-1]:
        qml.CNOT(wires=[i, i+1])

    qml.RX(-np.pi/2., wires=0)
    for i in range(1, 4):
        qml.Hadamard(wires=i)


def multi_run_wrapper(args):
    '''
    Wrapper function for classical AFQMC
    '''
    return ImagTimePropagator(*args)


def multi_run_wrapper_QAEE(args):
    '''
    Wrapper function for quantum AFQMC
    '''    
    return ImagTimePropagator_QAEE(*args)




def qImagTimePropagator(v_0, v_gamma, mf_shift, dtau, walker, weight, ovlp, h1e, eri, enuc, E_shift, h_chem, lambda_l, U_l, 
                        V_T, dev):
    '''This function defines the consistent imaginary propagation process on quantum computer, and will return new walker state 
       and new weight.
    
    Args:
        v_0: modified one-body term from reordering the two-body operator + mean-field subtraction.
        v_gamma: Cholesky vectors stored in list (L, num_spin_orbitals, num_spin_orbitals), without mf_shift
        mf_shift: mean-field shift \Bar{v}_{\gamma} stored in np.array format
        dtau: imaginary time step size
        walker: normalized walker state as np.ndarray, others are the same as trial
        weight:
        ovlp: the overlap between quantum trial state and walker
        h1e, eri: one-electron and two-electron integral stored in spatial orbitals
        enuc: nuclear repulsion energy
        E_shift: reference energy, usually taken as the HF energy.
        h_chem: modified one-body term from reordering the two-body operator
        lambda_l: eigenvalues of Cholesky vectors
        U_l: eigenvectors of Cholesky vectors
        V_T: quantum trial state
        dev: qml.device('lightning.qubit', wires=wires) for simulator;
             qml.device('braket.aws.qubit', device_arn=device_arn, wires=wires, shots=shots) for real device;
    
    Returns:
        E_loc:
        new_ovlp:
        new_weight:
        new_walker:
        
    '''
    seed = np.random.seed(int.from_bytes(os.urandom(4), byteorder='little'))
    
    # First compute the bias force using the expectation value of L operators
    num_spin_orbitals, num_electrons = walker.shape
    num_fields = len(v_gamma)
    I = np.identity(num_spin_orbitals)
    
    # consistent quantum-assisted energy evaluation     
    E_loc = (local_energy_quantum(walker, ovlp, h_chem, lambda_l, U_l, V_T, dev) / ovlp + enuc)
    
    # update the walker
    x = np.random.normal(0., 1., size=num_fields)
    new_walker = qPropagateWalker(x, v_0, v_gamma, mf_shift, dtau, walker, V_T, ovlp, dev)
    
    # Define the I operator and find new weight
    new_ovlp = amplitude_estimate(new_walker, V_T, dev)
    arg = np.angle(new_ovlp / ovlp)
    new_weight = weight*np.exp(-dtau*(np.real(E_loc) - E_shift))*np.max([0., np.cos(arg)])
    
    return E_loc, new_ovlp, new_walker, new_weight





def qAFQMC(num_walkers, num_steps, q_total_time, v_0, v_gamma, mf_shift, dtau, trial, h1e, eri, enuc, Ehf,
           h_chem, lambda_l, U_l, dev, max_pool, progress_bar=True):
    '''
    Args:
        num_walkers: size of the total samples
        num_steps: imaginary time steps taken
        q_total_time: list that stores the specific time where energy evaluation on quantum simulator is invoked.
        v_0: modified one-body term from reordering the two-body operator + mean-field subtraction.
        v_gamma: Cholesky vectors stored in list (L, num_spin_orbitals, num_spin_orbitals), without mf_shift
        mf_shift: mean-field shift \Bar{v}_{\gamma} stored in np.array format
        dtau: imaginary time step size
        trial: trial state as np.ndarray, e.g., for h2 HartreeFock state, it is np.array([[1,0], [0,1], [0,0], [0,0]])
        walker: normalized walker state as np.ndarray, others are the same as trial
        weight:
        h1e, eri: one-electron and two-electron integral stored in spatial orbitals
        enuc: nuclear repulsion energy
        Ehf: reference energy, usually taken as the HF energy.
        h_chem: modified one-body term from reordering the two-body operator
        lambda_l: eigenvalues of Cholesky vectors
        U_l: eigenvectors of Cholesky vectors
        dev: qml.device('lightning.qubit', wires=wires) for simulator;
        max_pool: number of cores for parallelization
    Returns:
    
    '''
    cE_list = []
    qE_list = []
    E_shift = Ehf
    total_time = np.linspace(dtau, int(dtau*num_steps), num=num_steps)
    walkers = [trial]*num_walkers
    weights = [1.0]*num_walkers
    # t = 0.0
    t_step = 0
    
    def generator():
        # while t < int(dtau*num_steps):
        while t_step <= num_steps:
            yield   
            
    for _ in tqdm(generator(), disable=not progress_bar):
        t = t_step * dtau
        weight_list = []
        walker_list = []
        cenergy_list = []
        qenergy_list = []
        ovlpratio_list = []

        inputs = []
        if np.round(t, 4) in q_total_time:
            for i in range(len(weights)):
                inputs.append((v_0, v_gamma, mf_shift, dtau, trial, walkers[i], weights[i],
                               h1e, eri, enuc, E_shift, h_chem, lambda_l, U_l, V_T, dev))

            with mp.Pool(max_pool) as pool:
                results = list(pool.map(multi_run_wrapper_QAEE, inputs))

            for (E_loc, E_loc_q, ovlp_ratio, new_walker, new_weight) in results:
                cenergy_list.append(E_loc)
                ovlpratio_list.append(ovlp_ratio)
                qenergy_list.append(E_loc_q)
                if new_weight > 1E-16:
                    weight_list.append(new_weight)
                    walker_list.append(new_walker)

            numerator = 0.+0.j
            denominator = 0.+0.j
            for i in range(len(weights)):
                numerator += weights[i]*qenergy_list[i]
                denominator += weights[i]*ovlpratio_list[i]    
            qE = np.real(numerator / denominator)    
            qE_list.append(qE)
            if not progress_bar:
                log_metric(metric_name="qE_list", value=qE, iteration_number=t_step)            

        else:
            for i in range(len(weights)):
                inputs.append((v_0, v_gamma, mf_shift, dtau, trial, walkers[i], weights[i],
                               h1e, eri, enuc, E_shift))

            with mp.Pool(max_pool) as pool:
                results = list(pool.map(multi_run_wrapper, inputs))

            for (E_loc, new_walker, new_weight) in results:
                cenergy_list.append(E_loc)
                if new_weight > 1E-16:
                    weight_list.append(new_weight)
                    walker_list.append(new_walker)

        E = np.real(np.average(cenergy_list, weights=weights))
        cE_list.append(E)
        E_shift = E
        if not progress_bar:
            log_metric(metric_name="cE_list", value=E, iteration_number=t_step)        

        # t += dtau
        t_step += 1
        walkers = walker_list
        weights = weight_list
        
    return total_time, cE_list, qE_list